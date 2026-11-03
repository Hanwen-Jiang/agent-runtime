// Package s3objects 是 ObjectStore 的 S3 兼容实现：Cloudflare R2、AWS S3、MinIO（设计书 4.7；docs/plan/cloud-deploy.md D-09）。
// 语义见 providers/objectstore；Python 侧见 agentruntime_core.adapters.s3_object_store，两边布局与语义一致，同一个桶两边可以互读。
// 只用标准库（签名见 sigv4.go）。
//
//   - 布局：一个桶，对象名是 [Prefix]<作用域>/<键>（作用域是租户 ID 或 _platform；Prefix 可选，几个环境共用一个桶时用，
//     各环境要么都带前缀、要么都不带）；路径式地址 <Endpoint>/<Bucket>/<对象名>。
//   - 元数据：内容的 sha256 放 x-amz-meta-ar-sha256，调用方的 Metadata 编成 base64 的 JSON 放 x-amz-meta-ar-metadata；
//     创建时间取 Last-Modified。
//   - IfAbsent 用条件写（If-None-Match: *），已存在时 412 → Conflict。
//   - Delete 先 Head 再删；List 用 ListObjectsV2，再逐个 Head 补全元数据；Limit 超过 1000 时按 1000 取。
//   - TemporaryURL 是 SigV4 预签名地址（最长 7 天）；经它上传的对象没有 sha256 元数据，Head 时 SHA256 为空串，Get 时按内容算。
//   - SetLifecycle 改写桶的生命周期配置里本作用域的规则（规则 ID 是 ar-<前缀与作用域的哈希>-<序号>，别的作用域的规则不动）；
//     S3 的过期只按整天算，不足一天按一天。清理由存储服务每天做，不能立即触发。
package s3objects

import (
	"bytes"
	"context"
	"crypto/md5"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"encoding/xml"
	"errors"
	"fmt"
	"io"
	"math"
	"net/http"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/clock"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/devutil"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/diskobjects"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/objectstore"
	"github.com/agentruntime/agent-runtime/libs/go-core/secret"
	"github.com/agentruntime/agent-runtime/libs/go-core/tenancy"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

const (
	metaSHA    = "X-Amz-Meta-Ar-Sha256"
	metaUser   = "X-Amz-Meta-Ar-Metadata"
	maxPresign = 7 * 24 * 3600
	s3NS       = "http://s3.amazonaws.com/doc/2006-03-01/"
)

// Options 是构造参数。SecretAccessKey 从 SecretStore 取（平台作用域），不写进配置文件。
type Options struct {
	Endpoint        string
	Bucket          string
	AccessKeyID     string
	SecretAccessKey secret.Value
	Region          string // 默认 auto（R2）
	Prefix          string // 可选，以 / 结尾
	Clock           clock.Clock
	Faults          *testkit.Faults
	HTTPClient      *http.Client
}

// 进程配置的环境变量（task-service、Worker 相同，Python 侧见 S3Settings.from_env）：设了 EnvEndpoint 就用这个实现，
// 不再用 AR_OBJECT_STORE_DIR。密钥给 EnvSecretAccessKey，或者 EnvSecretAccessKeyFile（K8s Secret 挂成的文件）。
const (
	EnvEndpoint            = "AR_S3_ENDPOINT"
	EnvBucket              = "AR_S3_BUCKET"
	EnvRegion              = "AR_S3_REGION"
	EnvPrefix              = "AR_S3_PREFIX"
	EnvAccessKeyID         = "AR_S3_ACCESS_KEY_ID"
	EnvSecretAccessKey     = "AR_S3_SECRET_ACCESS_KEY"
	EnvSecretAccessKeyFile = "AR_S3_SECRET_ACCESS_KEY_FILE"
)

// OptionsFromEnv 读取进程配置；没设 EnvEndpoint 时 ok 为 false。错误信息只提变量名，不带取值。
func OptionsFromEnv(getenv func(string) string) (opts Options, ok bool, err error) {
	endpoint := strings.TrimSpace(getenv(EnvEndpoint))
	if endpoint == "" {
		return Options{}, false, nil
	}
	key := getenv(EnvSecretAccessKey)
	if file := strings.TrimSpace(getenv(EnvSecretAccessKeyFile)); key == "" && file != "" {
		raw, err := os.ReadFile(file)
		if err != nil {
			return Options{}, false, fmt.Errorf("读不了 %s 指向的文件", EnvSecretAccessKeyFile)
		}
		key = strings.TrimSpace(string(raw))
	}
	var missing []string
	for name, v := range map[string]string{EnvBucket: getenv(EnvBucket), EnvAccessKeyID: getenv(EnvAccessKeyID), EnvSecretAccessKey: key} {
		if strings.TrimSpace(v) == "" {
			missing = append(missing, name)
		}
	}
	if len(missing) > 0 {
		sort.Strings(missing)
		return Options{}, false, fmt.Errorf("设了 %s 还要设 %s", EnvEndpoint, strings.Join(missing, "、"))
	}
	return Options{
		Endpoint: endpoint, Bucket: strings.TrimSpace(getenv(EnvBucket)), AccessKeyID: strings.TrimSpace(getenv(EnvAccessKeyID)),
		SecretAccessKey: secret.New(key), Region: strings.TrimSpace(getenv(EnvRegion)), Prefix: strings.TrimSpace(getenv(EnvPrefix)),
	}, true, nil
}

// Provider 是 S3 兼容 ObjectStore 的工厂。
type Provider struct {
	opts   Options
	clock  clock.Clock
	client *http.Client
}

// New 构造一个 S3 兼容的 ObjectStore。
func New(opts Options) (*Provider, error) {
	if opts.Endpoint == "" || opts.Bucket == "" || opts.AccessKeyID == "" || opts.SecretAccessKey.IsZero() {
		return nil, errors.New("s3objects: Endpoint、Bucket、AccessKeyID 与 SecretAccessKey 必填")
	}
	if !strings.HasPrefix(opts.Endpoint, "https://") && !strings.HasPrefix(opts.Endpoint, "http://127.0.0.1") && !strings.HasPrefix(opts.Endpoint, "http://localhost") {
		return nil, errors.New("s3objects: 地址要是 https（本机测试除外）")
	}
	if opts.Prefix != "" && (!strings.HasSuffix(opts.Prefix, "/") || strings.HasPrefix(opts.Prefix, "/") || strings.Contains(opts.Prefix, `\`)) {
		return nil, errors.New("s3objects: Prefix 要以 / 结尾，不得以 / 开头，不得含反斜杠")
	}
	opts.Endpoint = strings.TrimRight(opts.Endpoint, "/")
	if opts.Region == "" {
		opts.Region = "auto"
	}
	p := &Provider{opts: opts, clock: opts.Clock, client: opts.HTTPClient}
	if p.clock == nil {
		p.clock = clock.System{}
	}
	if p.client == nil {
		p.client = &http.Client{Timeout: 60 * time.Second, Transport: &http.Transport{Proxy: nil}}
	}
	return p, nil
}

func (p *Provider) Bind(t *commonv1.TenantContext) (objectstore.Store, error) {
	s, err := tenancy.TenantScope(t)
	if err != nil {
		return nil, err
	}
	return p.store(devutil.ScopeID(s)), nil
}

func (p *Provider) BindPlatform(purpose string) (objectstore.Store, error) {
	s, err := tenancy.PlatformScope(purpose)
	if err != nil {
		return nil, err
	}
	return p.store(devutil.ScopeID(s)), nil
}

func (p *Provider) Close() error {
	p.client.CloseIdleConnections()
	return nil
}

func (p *Provider) store(owner string) *store {
	return &store{p: p, owner: owner, base: p.opts.Prefix + owner + "/"}
}

// URL 是对象（name 非空）或桶的地址。
func (p *Provider) URL(name, query string) string {
	u := p.opts.Endpoint + "/" + p.opts.Bucket
	if name != "" {
		u += "/" + Escape(name, true)
	}
	if query != "" {
		u += "?" + query
	}
	return u
}

type response struct {
	status int
	header http.Header
	body   []byte
}

func (p *Provider) do(ctx context.Context, method, rawURL string, body []byte, header map[string]string) (response, error) {
	req, err := http.NewRequestWithContext(ctx, method, rawURL, bytes.NewReader(body))
	if err != nil {
		return response{}, providererr.Wrap(providererr.Invalid, "object_store_invalid", "对象存储的地址不对", err)
	}
	if body == nil {
		req.Body, req.ContentLength = http.NoBody, 0
	}
	for k, v := range header {
		req.Header.Set(k, v)
	}
	Sign(req, hashHex(body), p.opts.AccessKeyID, p.opts.SecretAccessKey, p.opts.Region, "s3", p.clock.Now())
	resp, err := p.client.Do(req)
	if err != nil {
		if ctx.Err() != nil {
			return response{}, providererr.Wrap(providererr.Transient, "object_store_unavailable", "请求对象存储被取消或超时", ctx.Err())
		}
		// 不带上 err 的原文：里面有完整地址，预签名地址的查询串里有签名。
		return response{}, providererr.New(providererr.Transient, "object_store_unavailable", fmt.Sprintf("连不上对象存储（%T）", err))
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return response{}, providererr.New(providererr.Transient, "object_store_unavailable", "读取对象存储的响应失败")
	}
	return response{status: resp.StatusCode, header: resp.Header, body: data}, nil
}

func errorFor(r response, what string) error {
	var e struct {
		Code string `xml:"Code"`
	}
	_ = xml.Unmarshal(r.body, &e)
	code := e.Code
	if code == "" {
		code = fmt.Sprint(r.status)
	}
	switch {
	case r.status == http.StatusNotFound:
		return providererr.New(providererr.NotFound, "not_found", what+"不存在")
	case r.status == http.StatusPreconditionFailed:
		return providererr.New(providererr.Conflict, "already_exists", "键已存在")
	case r.status == http.StatusUnauthorized || r.status == http.StatusForbidden:
		return providererr.New(providererr.Denied, "object_store_denied", "对象存储拒绝了请求（"+code+"）")
	case r.status == 400 || r.status == 405 || r.status == 411 || r.status == 413:
		return providererr.New(providererr.Invalid, "object_store_invalid", "对象存储认为请求不对（"+code+"）")
	default:
		return providererr.New(providererr.Transient, "object_store_unavailable", "对象存储暂时不可用（"+code+"）")
	}
}

// RuleIDPrefix 是一个作用域（含前缀）的生命周期规则 ID 前缀；用哈希，免得 t-a 的前缀成了 t-a-b 的前缀。Python 侧算法相同。
func RuleIDPrefix(base string) string { return "ar-" + hashHex([]byte(base))[:24] + "-" }

type store struct {
	p     *Provider
	owner string
	base  string
}

func (s *store) name(key string) (string, error) {
	if err := diskobjects.CheckKey(key); err != nil {
		return "", err
	}
	return s.base + key, nil
}

func (s *store) URI(key string) string { return "obj://" + s.owner + "/" + key }

func (s *store) KeyOf(uri string) (string, error) { return diskobjects.KeyOfURI(uri, s.owner) }

func (s *store) info(key string, h http.Header, data []byte) objectstore.ObjectInfo {
	sha := h.Get(metaSHA)
	if sha == "" && data != nil {
		sha = hashHex(data)
	}
	metadata := map[string]string{}
	if raw := h.Get(metaUser); raw != "" {
		if decoded, err := base64.StdEncoding.DecodeString(raw); err == nil {
			_ = json.Unmarshal(decoded, &metadata)
		}
	}
	created, err := http.ParseTime(h.Get("Last-Modified"))
	if err != nil {
		created = s.p.clock.Now()
	}
	size := int64(len(data))
	if data == nil {
		fmt.Sscan(h.Get("Content-Length"), &size)
	}
	contentType := h.Get("Content-Type")
	if contentType == "" {
		contentType = "application/octet-stream"
	}
	return objectstore.ObjectInfo{Key: key, URI: s.URI(key), Size: size, SHA256: sha, ContentType: contentType, CreatedAt: created.UTC(), Metadata: metadata}
}

func (s *store) Put(ctx context.Context, key string, data []byte, opts objectstore.PutOptions) (objectstore.ObjectInfo, error) {
	name, err := s.name(key)
	if err != nil {
		return objectstore.ObjectInfo{}, err
	}
	if err := s.p.opts.Faults.Check("Put"); err != nil {
		return objectstore.ObjectInfo{}, err
	}
	contentType := opts.ContentType
	if contentType == "" {
		contentType = "application/octet-stream"
	}
	sha := hashHex(data)
	header := map[string]string{"Content-Type": contentType, metaSHA: sha}
	metadata := map[string]string{}
	for k, v := range opts.Metadata {
		metadata[k] = v
	}
	if len(metadata) > 0 {
		raw, _ := json.Marshal(metadata)
		header[metaUser] = base64.StdEncoding.EncodeToString(raw)
	}
	if opts.IfAbsent {
		header["If-None-Match"] = "*"
	}
	if data == nil {
		data = []byte{}
	}
	r, err := s.p.do(ctx, http.MethodPut, s.p.URL(name, ""), data, header)
	if err != nil {
		return objectstore.ObjectInfo{}, err
	}
	if r.status != http.StatusOK && r.status != http.StatusCreated {
		return objectstore.ObjectInfo{}, errorFor(r, "对象")
	}
	return objectstore.ObjectInfo{Key: key, URI: s.URI(key), Size: int64(len(data)), SHA256: sha, ContentType: contentType,
		CreatedAt: s.p.clock.Now().UTC(), Metadata: metadata}, nil
}

func (s *store) Get(ctx context.Context, key string) (objectstore.ObjectData, error) {
	name, err := s.name(key)
	if err != nil {
		return objectstore.ObjectData{}, err
	}
	if err := s.p.opts.Faults.Check("Get"); err != nil {
		return objectstore.ObjectData{}, err
	}
	r, err := s.p.do(ctx, http.MethodGet, s.p.URL(name, ""), nil, nil)
	if err != nil {
		return objectstore.ObjectData{}, err
	}
	if r.status != http.StatusOK {
		return objectstore.ObjectData{}, errorFor(r, "对象")
	}
	return objectstore.ObjectData{Info: s.info(key, r.header, r.body), Data: r.body}, nil
}

func (s *store) Head(ctx context.Context, key string) (objectstore.ObjectInfo, error) {
	name, err := s.name(key)
	if err != nil {
		return objectstore.ObjectInfo{}, err
	}
	if err := s.p.opts.Faults.Check("Head"); err != nil {
		return objectstore.ObjectInfo{}, err
	}
	return s.head(ctx, key, name)
}

func (s *store) head(ctx context.Context, key, name string) (objectstore.ObjectInfo, error) {
	r, err := s.p.do(ctx, http.MethodHead, s.p.URL(name, ""), nil, nil)
	if err != nil {
		return objectstore.ObjectInfo{}, err
	}
	if r.status != http.StatusOK {
		return objectstore.ObjectInfo{}, errorFor(r, "对象")
	}
	return s.info(key, r.header, nil), nil
}

func (s *store) Delete(ctx context.Context, key string) (bool, error) {
	name, err := s.name(key)
	if err != nil {
		return false, err
	}
	if err := s.p.opts.Faults.Check("Delete"); err != nil {
		return false, err
	}
	if _, err := s.head(ctx, key, name); providererr.CategoryOf(err) == providererr.NotFound {
		return false, nil
	} else if err != nil {
		return false, err
	}
	r, err := s.p.do(ctx, http.MethodDelete, s.p.URL(name, ""), nil, nil)
	if err != nil {
		return false, err
	}
	if r.status != http.StatusOK && r.status != http.StatusNoContent && r.status != http.StatusNotFound {
		return false, errorFor(r, "对象")
	}
	return true, nil
}

type listResult struct {
	IsTruncated           bool   `xml:"IsTruncated"`
	NextContinuationToken string `xml:"NextContinuationToken"`
	Contents              []struct {
		Key string `xml:"Key"`
	} `xml:"Contents"`
}

func (s *store) List(ctx context.Context, opts objectstore.ListOptions) (objectstore.ObjectPage, error) {
	if err := s.p.opts.Faults.Check("List"); err != nil {
		return objectstore.ObjectPage{}, err
	}
	limit := opts.Limit
	if limit == 0 {
		limit = 1000
	}
	if limit < 0 {
		return objectstore.ObjectPage{}, providererr.New(providererr.Invalid, "invalid_argument", "Limit 不能为负")
	}
	if strings.HasPrefix(opts.Prefix, "/") || strings.Contains(opts.Prefix, `\`) {
		return objectstore.ObjectPage{}, providererr.New(providererr.Invalid, "invalid_key", "前缀不得以 / 开头，不得含反斜杠")
	}
	params := map[string]string{"list-type": "2", "prefix": s.base + opts.Prefix, "max-keys": fmt.Sprint(min(limit, 1000))}
	if opts.Cursor != "" {
		params["continuation-token"] = opts.Cursor
	}
	names := make([]string, 0, len(params))
	for k := range params {
		names = append(names, k)
	}
	sort.Strings(names)
	parts := make([]string, len(names))
	for i, k := range names {
		parts[i] = Escape(k, false) + "=" + Escape(params[k], false)
	}
	r, err := s.p.do(ctx, http.MethodGet, s.p.URL("", strings.Join(parts, "&")), nil, nil)
	if err != nil {
		return objectstore.ObjectPage{}, err
	}
	if r.status != http.StatusOK {
		return objectstore.ObjectPage{}, errorFor(r, "桶")
	}
	var result listResult
	if err := xml.Unmarshal(r.body, &result); err != nil {
		return objectstore.ObjectPage{}, providererr.New(providererr.Transient, "object_store_unavailable", "对象存储的列表响应无法解析")
	}
	var keys []string
	for _, c := range result.Contents {
		if strings.HasPrefix(c.Key, s.base) {
			keys = append(keys, strings.TrimPrefix(c.Key, s.base))
		}
	}
	infos := make([]*objectstore.ObjectInfo, len(keys))
	errs := make([]error, len(keys))
	var wg sync.WaitGroup
	slots := make(chan struct{}, 16)
	for i, key := range keys {
		wg.Add(1)
		go func() {
			defer wg.Done()
			slots <- struct{}{}
			defer func() { <-slots }()
			info, err := s.head(ctx, key, s.base+key)
			if providererr.CategoryOf(err) == providererr.NotFound {
				return // 列出之后被删掉了
			}
			infos[i], errs[i] = &info, err
		}()
	}
	wg.Wait()
	var page objectstore.ObjectPage
	for i := range keys {
		if errs[i] != nil {
			return objectstore.ObjectPage{}, errs[i]
		}
		if infos[i] != nil {
			page.Items = append(page.Items, *infos[i])
		}
	}
	if result.IsTruncated {
		page.NextCursor = result.NextContinuationToken
	}
	return page, nil
}

func (s *store) TemporaryURL(_ context.Context, key string, ttl time.Duration, method string) (string, error) {
	name, err := s.name(key)
	if err != nil {
		return "", err
	}
	if err := devutil.CheckPositive(ttl, "ttl"); err != nil {
		return "", err
	}
	if method != http.MethodGet && method != http.MethodPut {
		return "", providererr.New(providererr.Invalid, "invalid_argument", "method 只能是 GET 或 PUT")
	}
	if err := s.p.opts.Faults.Check("TemporaryURL"); err != nil {
		return "", err
	}
	seconds := min(maxPresign, max(1, int(math.Ceil(ttl.Seconds()))))
	return Presign(method, s.p.URL(name, ""), seconds, s.p.opts.AccessKeyID, s.p.opts.SecretAccessKey, s.p.opts.Region, "s3", s.p.clock.Now())
}

// SignedUpload 签进 x-amz-checksum-sha256（存储按内容核对）、If-None-Match: *（不覆盖）与 Content-Length（STOR-SEC）。
// 上线前要在选定的存储上实测这三个头是否被强制执行（AR_LIVE_S3 的一致性测试）。
func (s *store) SignedUpload(_ context.Context, key string, ttl time.Duration, sha256 string, size int64) (objectstore.SignedUpload, error) {
	name, err := s.name(key)
	if err != nil {
		return objectstore.SignedUpload{}, err
	}
	if err := objectstore.CheckSignedUpload(sha256, size, ttl); err != nil {
		return objectstore.SignedUpload{}, err
	}
	if err := s.p.opts.Faults.Check("SignedUpload"); err != nil {
		return objectstore.SignedUpload{}, err
	}
	digest, _ := hex.DecodeString(sha256)
	headers := map[string]string{
		"Content-Length": strconv.FormatInt(size, 10), "If-None-Match": "*", "x-amz-checksum-sha256": base64.StdEncoding.EncodeToString(digest),
	}
	seconds := int(math.Ceil(ttl.Seconds()))
	u, err := PresignHeaders(http.MethodPut, s.p.URL(name, ""), seconds, s.p.opts.AccessKeyID, s.p.opts.SecretAccessKey, s.p.opts.Region, "s3", s.p.clock.Now(), headers)
	if err != nil {
		return objectstore.SignedUpload{}, err
	}
	return objectstore.SignedUpload{URL: u, Headers: headers}, nil
}

// 生命周期配置：解析时保留不认识的规则原样（innerxml），只替换本作用域的。
type lifecycleConfig struct {
	XMLName xml.Name        `xml:"LifecycleConfiguration"`
	Rules   []lifecycleRule `xml:"Rule"`
}

type lifecycleRule struct {
	ID    string `xml:"ID"`
	Inner string `xml:",innerxml"`
}

func (s *store) SetLifecycle(ctx context.Context, rules []objectstore.LifecycleRule) error {
	for _, r := range rules {
		if err := devutil.CheckPositive(r.ExpireAfter, "ExpireAfter"); err != nil {
			return err
		}
		if strings.HasPrefix(r.Prefix, "/") || strings.Contains(r.Prefix, `\`) {
			return providererr.New(providererr.Invalid, "invalid_key", "前缀不得以 / 开头，不得含反斜杠")
		}
	}
	if err := s.p.opts.Faults.Check("SetLifecycle"); err != nil {
		return err
	}
	u := s.p.URL("", "lifecycle=")
	mine := RuleIDPrefix(s.base)
	r, err := s.p.do(ctx, http.MethodGet, u, nil, nil)
	if err != nil {
		return err
	}
	var kept []string
	switch r.status {
	case http.StatusOK:
		var cfg lifecycleConfig
		if err := xml.Unmarshal(r.body, &cfg); err != nil {
			return providererr.New(providererr.Transient, "object_store_unavailable", "桶的生命周期配置无法解析")
		}
		for _, rule := range cfg.Rules {
			if !strings.HasPrefix(rule.ID, mine) {
				kept = append(kept, "<Rule>"+rule.Inner+"</Rule>")
			}
		}
	case http.StatusNotFound:
	default:
		return errorFor(r, "桶的生命周期配置")
	}
	var b strings.Builder
	for i, rule := range rules {
		days := max(1, int(math.Ceil(rule.ExpireAfter.Hours()/24)))
		var prefix bytes.Buffer
		_ = xml.EscapeText(&prefix, []byte(s.base+rule.Prefix))
		fmt.Fprintf(&b, "<Rule><ID>%s%d</ID><Filter><Prefix>%s</Prefix></Filter><Status>Enabled</Status><Expiration><Days>%d</Days></Expiration></Rule>",
			mine, i, prefix.String(), days)
	}
	if len(kept) == 0 && len(rules) == 0 {
		r, err := s.p.do(ctx, http.MethodDelete, u, nil, nil)
		if err != nil {
			return err
		}
		if r.status != http.StatusOK && r.status != http.StatusNoContent && r.status != http.StatusNotFound {
			return errorFor(r, "桶的生命周期配置")
		}
		return nil
	}
	body := []byte(`<?xml version="1.0" encoding="UTF-8"?><LifecycleConfiguration xmlns="` + s3NS + `">` + strings.Join(kept, "") + b.String() + "</LifecycleConfiguration>")
	// S3 要求生命周期配置带 Content-MD5（只是完整性校验，不是安全用途）。
	sum := md5.Sum(body)
	r, err = s.p.do(ctx, http.MethodPut, u, body, map[string]string{"Content-Type": "application/xml", "Content-MD5": base64.StdEncoding.EncodeToString(sum[:])})
	if err != nil {
		return err
	}
	if r.status != http.StatusOK && r.status != http.StatusNoContent {
		return errorFor(r, "桶的生命周期配置")
	}
	return nil
}
