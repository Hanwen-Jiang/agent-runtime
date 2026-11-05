// Package envbuildtest 是测试用的 OCI 打包与假镜像仓库（从 py-core 的 agentruntime_core.adapters.ocitest 搬来；envbuild 与服务的测试共用）。
package envbuildtest

import (
	"archive/tar"
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"regexp"
	"sync"
	"testing"
)

// DigestOf 是内容的 sha256 digest。
func DigestOf(data []byte) string {
	sum := sha256.Sum256(data)
	return "sha256:" + hex.EncodeToString(sum[:])
}

// TarOptions：Nested 时 index 指向一个嵌套的 index；Tamper 时第一层的内容与 digest 对不上。
type TarOptions struct {
	Nested, Tamper bool
}

// OCITar 是一个最小的 OCI 打包（BuildKit --output type=oci 的结构）；layers 为 nil 时两层。
func OCITar(layers [][]byte, o TarOptions) []byte {
	if layers == nil {
		layers = [][]byte{[]byte("layer-one"), bytes.Repeat([]byte("layer-two"), 10)}
	}
	config, _ := json.Marshal(map[string]any{"architecture": "amd64", "os": "linux", "rootfs": map[string]any{"type": "layers", "diff_ids": []any{}}})
	var layerDescs []any
	for _, l := range layers {
		layerDescs = append(layerDescs, map[string]any{"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip", "digest": DigestOf(l), "size": len(l)})
	}
	manifest, _ := json.Marshal(map[string]any{"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
		"config": map[string]any{"mediaType": "application/vnd.oci.image.config.v1+json", "digest": DigestOf(config), "size": len(config)}, "layers": layerDescs})
	type blob struct {
		digest string
		data   []byte
	}
	blobs := []blob{{DigestOf(config), config}, {DigestOf(manifest), manifest}}
	for _, l := range layers {
		blobs = append(blobs, blob{DigestOf(l), l})
	}
	entry := map[string]any{"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": DigestOf(manifest), "size": len(manifest),
		"platform": map[string]any{"os": "linux", "architecture": "amd64"}}
	index := map[string]any{"schemaVersion": 2, "manifests": []any{entry}}
	if o.Nested {
		inner, _ := json.Marshal(map[string]any{"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": []any{entry}})
		blobs = append(blobs, blob{DigestOf(inner), inner})
		index = map[string]any{"schemaVersion": 2, "manifests": []any{map[string]any{"mediaType": "application/vnd.oci.image.index.v1+json",
			"digest": DigestOf(inner), "size": len(inner)}}}
	}
	var out bytes.Buffer
	tw := tar.NewWriter(&out)
	add := func(name string, data []byte) {
		_ = tw.WriteHeader(&tar.Header{Name: name, Mode: 0o644, Size: int64(len(data)), Typeflag: tar.TypeReg})
		_, _ = tw.Write(data)
	}
	add("oci-layout", []byte(`{"imageLayoutVersion": "1.0.0"}`))
	indexJSON, _ := json.Marshal(index)
	add("index.json", indexJSON)
	seen := map[string]bool{}
	for _, b := range blobs {
		if seen[b.digest] {
			continue
		}
		seen[b.digest] = true
		data := b.data
		if o.Tamper && bytes.Equal(data, layers[0]) {
			data = bytes.Repeat([]byte("x"), len(data))
		}
		add("blobs/sha256/"+b.digest[len("sha256:"):], data)
	}
	_ = tw.Close()
	return out.Bytes()
}

// RepoKey 是（路径，标签或 digest）。
type RepoKey struct{ Repo, Ref string }

// Registry 是进程里的 OCI Distribution 仓库（只做推、查、删用到的几个接口），要 Basic 认证（pusher / s3cret）。
type Registry struct {
	URL       string
	mu        sync.Mutex
	Blobs     map[RepoKey][]byte
	Manifests map[RepoKey][]byte
	Deleted   []RepoKey
	Uploads   int
	auth      string
}

var (
	headBlob   = regexp.MustCompile(`^/v2/(.+)/blobs/(sha256:[0-9a-f]{64})$`)
	postUpload = regexp.MustCompile(`^/v2/(.+)/blobs/uploads/$`)
	putUpload  = regexp.MustCompile(`^/v2/(.+)/blobs/uploads/(u\d+)$`)
	manifestRe = regexp.MustCompile(`^/v2/(.+)/manifests/([^/]+)$`)
)

// NewRegistry 起一个，用例结束时关掉。
func NewRegistry(t testing.TB) *Registry {
	r := &Registry{Blobs: map[RepoKey][]byte{}, Manifests: map[RepoKey][]byte{},
		auth: "Basic " + base64.StdEncoding.EncodeToString([]byte("pusher:s3cret"))}
	srv := httptest.NewServer(http.HandlerFunc(r.serve))
	t.Cleanup(srv.Close)
	r.URL = srv.URL
	return r
}

func (r *Registry) serve(w http.ResponseWriter, req *http.Request) {
	if req.Header.Get("Authorization") != r.auth {
		w.WriteHeader(401)
		return
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	path := req.URL.Path
	switch req.Method {
	case http.MethodHead:
		if m := headBlob.FindStringSubmatch(path); m != nil {
			if _, ok := r.Blobs[RepoKey{m[1], m[2]}]; ok {
				w.WriteHeader(200)
				return
			}
		}
		w.WriteHeader(404)
	case http.MethodPost:
		m := postUpload.FindStringSubmatch(path)
		if m == nil {
			w.WriteHeader(404)
			return
		}
		r.Uploads++
		w.Header().Set("Location", fmt.Sprintf("/v2/%s/blobs/uploads/u%d", m[1], r.Uploads))
		w.WriteHeader(202)
	case http.MethodPut:
		body, _ := io.ReadAll(req.Body)
		if m := putUpload.FindStringSubmatch(path); m != nil {
			digest, _ := url.QueryUnescape(req.URL.Query().Get("digest"))
			if DigestOf(body) != digest {
				w.WriteHeader(400)
				return
			}
			r.Blobs[RepoKey{m[1], digest}] = body
			w.WriteHeader(201)
			return
		}
		if m := manifestRe.FindStringSubmatch(path); m != nil {
			r.Manifests[RepoKey{m[1], m[2]}] = body
			r.Manifests[RepoKey{m[1], DigestOf(body)}] = body
			w.Header().Set("Docker-Content-Digest", DigestOf(body))
			w.WriteHeader(201)
			return
		}
		w.WriteHeader(404)
	case http.MethodDelete:
		if m := manifestRe.FindStringSubmatch(path); m != nil {
			k := RepoKey{m[1], m[2]}
			if _, ok := r.Manifests[k]; ok {
				r.Deleted = append(r.Deleted, k)
				delete(r.Manifests, k)
				w.WriteHeader(202)
				return
			}
		}
		w.WriteHeader(404)
	}
}

// Repos 是推过 blob 的路径。
func (r *Registry) Repos() map[string]bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := map[string]bool{}
	for k := range r.Blobs {
		out[k.Repo] = true
	}
	return out
}

// Manifest 取一个清单。
func (r *Registry) Manifest(repo, ref string) ([]byte, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	v, ok := r.Manifests[RepoKey{repo, ref}]
	return v, ok
}

// DeletedList 是删过的清单。
func (r *Registry) DeletedList() []RepoKey {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]RepoKey{}, r.Deleted...)
}

// UploadCount 是开过的上传数。
func (r *Registry) UploadCount() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.Uploads
}
