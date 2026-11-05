package envbuild

// OCI 镜像的读与推（从 py-core 的 agentruntime_core.adapters.oci_registry 搬来）。只用标准库。
//
//   - ReadLayout：读构建器交来的 OCI 打包（BuildKit 的 --output type=oci，一个 tar：oci-layout、index.json、blobs/sha256/…）。
//     扫一遍记下每个条目在文件里的位置，不解开到磁盘；index 里有多个平台时取 linux/amd64（没写平台的取第一个）。每个 blob 的大小
//     按清单记的算，总和超过上限 invalid（image_too_large）；清单按 digest 核对，层在推的时候边读边核对。
//   - RegistryClient.Push：按 OCI Distribution 的协议推到仓库的一个路径（先 HEAD，已有的 blob 不重传；没有的 POST 开上传、
//     PUT 带 digest 一次传完），最后 PUT 清单，返回清单的 digest。仓库的凭据（Basic）只在这里用，不进日志与错误信息。
//     路径由调用方给（服务按租户拼），构建产物里写的名字与注解一概不看。
//   - Delete：按 digest 删清单（长期不用的镜像定期清掉；仓库要打开删除）。

import (
	"archive/tar"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strings"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
)

var (
	indexTypes    = []string{"application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json"}
	manifestTypes = []string{"application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"}
	digestRe      = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
	repoRe        = regexp.MustCompile(`^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*$`)
	tagRe         = regexp.MustCompile(`^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$`)
)

func invalidImage(msg string) error {
	return providererr.New(providererr.Invalid, "invalid_image", msg)
}

func contains(list []string, v string) bool {
	for _, x := range list {
		if x == v {
			return true
		}
	}
	return false
}

// Blob 是要推的一个 blob（config 或一层）。
type Blob struct {
	Digest    string
	Size      int64
	MediaType string
}

type entry struct{ offset, size int64 }

// Layout 是读好的 OCI 打包：清单的原文与要推的 blob。blob 的内容按需从文件里读。
type Layout struct {
	Manifest  []byte
	MediaType string
	Blobs     []Blob
	file      io.ReaderAt
	entries   map[string]entry
}

// Size 是各 blob 之和。
func (l *Layout) Size() int64 {
	var n int64
	for _, b := range l.Blobs {
		n += b.Size
	}
	return n
}

// ManifestDigest 是清单的 digest。
func (l *Layout) ManifestDigest() string {
	sum := sha256.Sum256(l.Manifest)
	return "sha256:" + hex.EncodeToString(sum[:])
}

// OpenBlob 读一个 blob 的内容。
func (l *Layout) OpenBlob(digest string) (*io.SectionReader, error) {
	name, err := blobName(digest)
	if err != nil {
		return nil, err
	}
	e, ok := l.entries[name]
	if !ok {
		return nil, invalidImage("打包里没有 " + digest)
	}
	return io.NewSectionReader(l.file, e.offset, e.size), nil
}

func blobName(digest string) (string, error) {
	if !digestRe.MatchString(digest) {
		return "", invalidImage("digest 的格式不对")
	}
	return "blobs/sha256/" + strings.TrimPrefix(digest, "sha256:"), nil
}

func (l *Layout) read(name string, limit int64) ([]byte, error) {
	e, ok := l.entries[name]
	if !ok {
		return nil, invalidImage("打包里没有 " + name)
	}
	if e.size > limit {
		return nil, invalidImage(name + " 不是普通文件或太大")
	}
	data := make([]byte, e.size)
	if _, err := l.file.ReadAt(data, e.offset); err != nil && !errors.Is(err, io.EOF) {
		return nil, invalidImage("读不了 " + name)
	}
	return data, nil
}

func (l *Layout) verified(digest string) ([]byte, error) {
	name, err := blobName(digest)
	if err != nil {
		return nil, err
	}
	data, err := l.read(name, 4<<20)
	if err != nil {
		return nil, err
	}
	sum := sha256.Sum256(data)
	if "sha256:"+hex.EncodeToString(sum[:]) != digest {
		return nil, invalidImage(digest + " 的内容与 digest 对不上")
	}
	return data, nil
}

type descriptor struct {
	MediaType string `json:"mediaType"`
	Digest    string `json:"digest"`
	Size      int64  `json:"size"`
	Platform  *struct {
		OS           string `json:"os"`
		Architecture string `json:"architecture"`
	} `json:"platform"`
}

func pick(entries []descriptor) (descriptor, error) {
	if len(entries) == 0 {
		return descriptor{}, invalidImage("清单列表是空的")
	}
	for _, e := range entries {
		if e.Platform != nil && e.Platform.OS == "linux" && e.Platform.Architecture == "amd64" {
			return e, nil
		}
	}
	return entries[0], nil
}

// ReadLayout 读 OCI 打包（f 要能按位置读：服务的临时文件）；超过 maxBytes（各 blob 之和）invalid（image_too_large）。
func ReadLayout(f *os.File, maxBytes int64) (*Layout, error) {
	if _, err := f.Seek(0, io.SeekStart); err != nil {
		return nil, invalidImage("读不了构建产物")
	}
	l := &Layout{file: f, entries: map[string]entry{}}
	tr := tar.NewReader(f)
	for {
		h, err := tr.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			if len(l.entries) == 0 {
				return nil, invalidImage("不是 tar 格式的 OCI 打包")
			}
			return nil, invalidImage("OCI 打包不完整")
		}
		if h.Typeflag != tar.TypeReg {
			continue
		}
		// archive/tar 不预读：Next 之后文件的位置就是这个条目内容的开头。
		pos, err := f.Seek(0, io.SeekCurrent)
		if err != nil {
			return nil, invalidImage("读不了构建产物")
		}
		l.entries[strings.TrimPrefix(h.Name, "./")] = entry{offset: pos, size: h.Size}
	}
	raw, err := l.read("index.json", 4<<20)
	if err != nil {
		return nil, err
	}
	var index struct {
		Manifests []descriptor `json:"manifests"`
	}
	if json.Unmarshal(raw, &index) != nil {
		return nil, invalidImage("index.json 不是合法的 JSON")
	}
	if len(index.Manifests) == 0 {
		return nil, invalidImage("index.json 里没有清单")
	}
	e, err := pick(index.Manifests)
	if err != nil {
		return nil, err
	}
	mediaType := e.MediaType
	data, err := l.verified(e.Digest)
	if err != nil {
		return nil, err
	}
	var nested struct {
		Manifests []descriptor `json:"manifests"`
	}
	_ = json.Unmarshal(data, &nested)
	if contains(indexTypes, mediaType) || len(nested.Manifests) > 0 {
		if e, err = pick(nested.Manifests); err != nil {
			return nil, err
		}
		mediaType = e.MediaType
		if data, err = l.verified(e.Digest); err != nil {
			return nil, err
		}
	}
	var manifest struct {
		MediaType string       `json:"mediaType"`
		Config    *descriptor  `json:"config"`
		Layers    []descriptor `json:"layers"`
	}
	if json.Unmarshal(data, &manifest) != nil || manifest.Config == nil {
		return nil, invalidImage("清单的格式不对")
	}
	if mediaType == "" {
		mediaType = manifest.MediaType
	}
	if mediaType == "" {
		mediaType = manifestTypes[0]
	}
	if !contains(manifestTypes, mediaType) {
		return nil, invalidImage("不认识的清单类型 " + mediaType)
	}
	blobs := []Blob{{Digest: manifest.Config.Digest, Size: manifest.Config.Size, MediaType: manifest.Config.MediaType}}
	for _, layer := range manifest.Layers {
		blobs = append(blobs, Blob{Digest: layer.Digest, Size: layer.Size, MediaType: layer.MediaType})
	}
	for _, b := range blobs {
		name, err := blobName(b.Digest)
		if err != nil {
			return nil, err
		}
		got, ok := l.entries[name]
		if !ok {
			return nil, invalidImage("打包里没有 " + b.Digest)
		}
		if got.size != b.Size {
			return nil, invalidImage(b.Digest + " 的大小与清单不符")
		}
	}
	l.Manifest, l.MediaType, l.Blobs = data, mediaType, blobs
	if size := l.Size(); size > maxBytes {
		return nil, providererr.New(providererr.Invalid, "image_too_large", fmt.Sprintf("镜像有 %d 字节，超过上限 %d", size, maxBytes))
	}
	return l, nil
}

// RegistryClient 是 OCI Distribution 仓库的客户端（推、删）。BaseURL 形如 http://registry.agent-runtime.svc:5000。
type RegistryClient struct {
	base string
	// PullHost 是镜像引用里写的主机（节点拉镜像用的名字，可以与推送用的集群内地址不同，例如单节点的 127.0.0.1:30500）。
	PullHost string
	auth     string
	client   *http.Client
}

// NewRegistryClient 构造；凭据为空时不认证。
func NewRegistryClient(baseURL, username, password, pullHost string) (*RegistryClient, error) {
	base := strings.TrimRight(baseURL, "/")
	u, err := url.Parse(base)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return nil, providererr.New(providererr.Invalid, "invalid_registry", "仓库地址要是 http 或 https")
	}
	if pullHost == "" {
		pullHost = u.Host
	}
	auth := ""
	if username != "" {
		auth = "Basic " + base64.StdEncoding.EncodeToString([]byte(username+":"+password))
	}
	// 不走代理：仓库在集群里。
	client := &http.Client{Timeout: 10 * time.Minute, Transport: &http.Transport{Proxy: nil}}
	return &RegistryClient{base: base, PullHost: pullHost, auth: auth, client: client}, nil
}

// Reference 是推好的镜像的引用（按 digest）。
func (c *RegistryClient) Reference(repo, digest string) string {
	return c.PullHost + "/" + repo + "@" + digest
}

func (c *RegistryClient) request(ctx context.Context, method, target string, body io.Reader, length int64, headers map[string]string) (int, http.Header, error) {
	if !strings.HasPrefix(target, "http") {
		target = c.base + target
	}
	req, err := http.NewRequestWithContext(ctx, method, target, body)
	if err != nil {
		return 0, nil, providererr.New(providererr.Invalid, "registry_rejected", "镜像仓库给的地址不对")
	}
	if body != nil {
		req.ContentLength = length
	} else if method == http.MethodPost || method == http.MethodPut {
		req.ContentLength = 0
		req.Body = http.NoBody
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	if c.auth != "" {
		req.Header.Set("Authorization", c.auth)
	}
	resp, err := c.client.Do(req)
	if err != nil {
		return 0, nil, providererr.New(providererr.Transient, "registry_unavailable", "连不上镜像仓库")
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<20))
	return resp.StatusCode, resp.Header, nil
}

func check(status int, what string) error {
	switch {
	case status == 401 || status == 403:
		return providererr.New(providererr.Invalid, "registry_denied", "镜像仓库拒绝了"+what+"（凭据或权限不对）")
	case status == 404:
		return providererr.New(providererr.NotFound, "registry_not_found", "镜像仓库里没有"+what)
	case status >= 500 || status == 429:
		return providererr.New(providererr.Transient, "registry_unavailable", fmt.Sprintf("镜像仓库暂时不可用（%s，HTTP %d）", what, status))
	case status >= 300:
		return providererr.New(providererr.Invalid, "registry_rejected", fmt.Sprintf("镜像仓库不接受%s（HTTP %d）", what, status))
	}
	return nil
}

// Push 推一个镜像，返回清单的 digest。
func (c *RegistryClient) Push(ctx context.Context, repo, tag string, l *Layout) (string, error) {
	if !repoRe.MatchString(repo) || !tagRe.MatchString(tag) {
		return "", providererr.New(providererr.Invalid, "invalid_registry_path", "仓库路径或标签的格式不对")
	}
	for _, b := range l.Blobs {
		status, _, err := c.request(ctx, http.MethodHead, "/v2/"+repo+"/blobs/"+b.Digest, nil, 0, nil)
		if err != nil {
			return "", err
		}
		if status == 200 {
			continue
		}
		status, headers, err := c.request(ctx, http.MethodPost, "/v2/"+repo+"/blobs/uploads/", nil, 0, nil)
		if err != nil {
			return "", err
		}
		if err := check(status, "开始上传"); err != nil {
			return "", err
		}
		location := headers.Get("Location")
		if location == "" {
			return "", providererr.New(providererr.Invalid, "registry_rejected", "镜像仓库没给上传地址")
		}
		if !strings.HasPrefix(location, "http") {
			location = c.base + location
		}
		sep := "?"
		if strings.Contains(location, "?") {
			sep = "&"
		}
		section, err := l.OpenBlob(b.Digest)
		if err != nil {
			return "", err
		}
		hash := sha256.New()
		status, _, err = c.request(ctx, http.MethodPut, location+sep+"digest="+url.QueryEscape(b.Digest), io.TeeReader(section, hash), b.Size,
			map[string]string{"Content-Type": "application/octet-stream"})
		if err != nil {
			return "", err
		}
		if "sha256:"+hex.EncodeToString(hash.Sum(nil)) != b.Digest {
			return "", invalidImage(b.Digest + " 的内容与 digest 对不上")
		}
		if err := check(status, "上传的层"); err != nil {
			return "", err
		}
	}
	status, headers, err := c.request(ctx, http.MethodPut, "/v2/"+repo+"/manifests/"+tag, strings.NewReader(string(l.Manifest)), int64(len(l.Manifest)),
		map[string]string{"Content-Type": l.MediaType})
	if err != nil {
		return "", err
	}
	if err := check(status, "清单"); err != nil {
		return "", err
	}
	digest := headers.Get("Docker-Content-Digest")
	if digest == "" {
		digest = l.ManifestDigest()
	}
	if digest != l.ManifestDigest() {
		return "", providererr.New(providererr.Invalid, "registry_rejected", "镜像仓库记下的清单 digest 与上传的不同")
	}
	return digest, nil
}

// Delete 删一个清单；本来就没有时返回 false。
func (c *RegistryClient) Delete(ctx context.Context, repo, digest string) (bool, error) {
	if !repoRe.MatchString(repo) || !digestRe.MatchString(digest) {
		return false, providererr.New(providererr.Invalid, "invalid_registry_path", "仓库路径或 digest 的格式不对")
	}
	status, _, err := c.request(ctx, http.MethodDelete, "/v2/"+repo+"/manifests/"+digest, nil, 0, nil)
	if err != nil {
		return false, err
	}
	if status == 404 {
		return false, nil
	}
	if err := check(status, "删除"); err != nil {
		return false, err
	}
	return true, nil
}
