// Package diskobjects 是本地磁盘 ObjectStore（开发实现）。语义见 providers/objectstore。
//
// 布局：<Root>/<作用域>/data/<键的 sha256>，元数据在同名的 .json 里。用键的哈希作文件名，避开 Windows
// 不区分大小写与保留文件名的问题。临时地址是本地签名的 devobj:// 地址，只能用 FetchURL、UploadURL 访问。
package diskobjects

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/clock"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/devutil"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/objectstore"
	"github.com/agentruntime/agent-runtime/libs/go-core/tenancy"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

var (
	slug    = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,62}$`)
	uriRe   = regexp.MustCompile(`(?s)^obj://([^/]+)/(.+)$`)
	control = regexp.MustCompile(`[\x00-\x1f\x7f]`)
)

// CheckKey 校验对象键。
func CheckKey(key string) error {
	if len(key) < 1 || len(key) > 1024 {
		return providererr.New(providererr.Invalid, "invalid_key", "键长度必须在 1 到 1024 之间")
	}
	if strings.HasPrefix(key, "/") || strings.Contains(key, `\`) || control.MatchString(key) {
		return providererr.New(providererr.Invalid, "invalid_key", "键不得以 / 开头，不得含反斜杠或控制字符")
	}
	for _, seg := range strings.Split(key, "/") {
		if seg == "" || seg == "." || seg == ".." {
			return providererr.New(providererr.Invalid, "invalid_key", "键不得有空段、. 或 .. 段")
		}
	}
	return nil
}

// Options 是构造参数。
type Options struct {
	Root       string
	SigningKey []byte
	Clock      clock.Clock
	Faults     *testkit.Faults
}

// Provider 是本地磁盘 ObjectStore 的工厂。
type Provider struct {
	mu     sync.Mutex
	opts   Options
	clock  clock.Clock
	faults *testkit.Faults
	rules  map[string][]objectstore.LifecycleRule
}

// New 构造一个本地磁盘 ObjectStore。
func New(opts Options) (*Provider, error) {
	if opts.Root == "" || len(opts.SigningKey) == 0 {
		return nil, errors.New("diskobjects: Root 与 SigningKey 必填")
	}
	c := opts.Clock
	if c == nil {
		c = clock.System{}
	}
	return &Provider{opts: opts, clock: c, faults: opts.Faults, rules: map[string][]objectstore.LifecycleRule{}}, nil
}

func (p *Provider) Bind(t *commonv1.TenantContext) (objectstore.Store, error) {
	s, err := tenancy.TenantScope(t)
	if err != nil {
		return nil, err
	}
	return &store{p: p, owner: devutil.ScopeID(s)}, nil
}

func (p *Provider) BindPlatform(purpose string) (objectstore.Store, error) {
	s, err := tenancy.PlatformScope(purpose)
	if err != nil {
		return nil, err
	}
	return &store{p: p, owner: devutil.ScopeID(s)}, nil
}

func (p *Provider) Close() error { return nil }

type meta struct {
	Key         string            `json:"key"`
	Size        int64             `json:"size"`
	SHA256      string            `json:"sha256"`
	ContentType string            `json:"content_type"`
	CreatedAt   time.Time         `json:"created_at"`
	Metadata    map[string]string `json:"metadata"`
}

func (p *Provider) dir(owner string) string { return filepath.Join(p.opts.Root, owner, "data") }

func (p *Provider) paths(owner, key string) (string, string) {
	sum := sha256.Sum256([]byte(key))
	name := hex.EncodeToString(sum[:])
	return filepath.Join(p.dir(owner), name), filepath.Join(p.dir(owner), name+".json")
}

func (p *Provider) info(owner string, m meta) objectstore.ObjectInfo {
	return objectstore.ObjectInfo{
		Key: m.Key, URI: "obj://" + owner + "/" + m.Key, Size: m.Size, SHA256: m.SHA256,
		ContentType: m.ContentType, CreatedAt: m.CreatedAt, Metadata: m.Metadata,
	}
}

func (p *Provider) expired(owner string, m meta) bool {
	now := p.clock.Now()
	for _, r := range p.rules[owner] {
		if strings.HasPrefix(m.Key, r.Prefix) && !m.CreatedAt.Add(r.ExpireAfter).After(now) {
			return true
		}
	}
	return false
}

// loadLocked 读元数据，顺带清理过期对象；不存在返回 false。
func (p *Provider) loadLocked(owner, key string) (meta, bool, error) {
	dataPath, metaPath := p.paths(owner, key)
	raw, err := os.ReadFile(metaPath)
	if errors.Is(err, fs.ErrNotExist) {
		return meta{}, false, nil
	}
	if err != nil {
		return meta{}, false, providererr.Wrap(providererr.Transient, "io_error", "读取元数据失败", err)
	}
	var m meta
	if err := json.Unmarshal(raw, &m); err != nil {
		return meta{}, false, providererr.Wrap(providererr.Transient, "io_error", "元数据损坏", err)
	}
	if p.expired(owner, m) {
		_ = os.Remove(metaPath)
		_ = os.Remove(dataPath)
		return meta{}, false, nil
	}
	return m, true, nil
}

func atomicWrite(path string, data []byte) error {
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

func (p *Provider) writeLocked(owner, key string, data []byte, opts objectstore.PutOptions) (objectstore.ObjectInfo, error) {
	if opts.IfAbsent {
		if _, ok, err := p.loadLocked(owner, key); err != nil {
			return objectstore.ObjectInfo{}, err
		} else if ok {
			return objectstore.ObjectInfo{}, providererr.New(providererr.Conflict, "already_exists", "对象已存在")
		}
	}
	if err := os.MkdirAll(p.dir(owner), 0o700); err != nil {
		return objectstore.ObjectInfo{}, providererr.Wrap(providererr.Transient, "io_error", "创建目录失败", err)
	}
	contentType := opts.ContentType
	if contentType == "" {
		contentType = "application/octet-stream"
	}
	metadata := map[string]string{}
	for k, v := range opts.Metadata {
		metadata[k] = v
	}
	sum := sha256.Sum256(data)
	m := meta{Key: key, Size: int64(len(data)), SHA256: hex.EncodeToString(sum[:]), ContentType: contentType,
		CreatedAt: p.clock.Now().UTC(), Metadata: metadata}
	raw, _ := json.Marshal(m)
	dataPath, metaPath := p.paths(owner, key)
	if err := atomicWrite(dataPath, data); err != nil {
		return objectstore.ObjectInfo{}, providererr.Wrap(providererr.Transient, "io_error", "写入对象失败", err)
	}
	if err := atomicWrite(metaPath, raw); err != nil {
		return objectstore.ObjectInfo{}, providererr.Wrap(providererr.Transient, "io_error", "写入元数据失败", err)
	}
	return p.info(owner, m), nil
}

func (p *Provider) readLocked(owner, key string) (objectstore.ObjectData, error) {
	m, ok, err := p.loadLocked(owner, key)
	if err != nil {
		return objectstore.ObjectData{}, err
	}
	if !ok {
		return objectstore.ObjectData{}, providererr.New(providererr.NotFound, "not_found", "对象不存在")
	}
	dataPath, _ := p.paths(owner, key)
	data, err := os.ReadFile(dataPath)
	if err != nil {
		return objectstore.ObjectData{}, providererr.Wrap(providererr.Transient, "io_error", "读取对象失败", err)
	}
	return objectstore.ObjectData{Info: p.info(owner, m), Data: data}, nil
}

func (p *Provider) allLocked(owner string) ([]meta, error) {
	files, err := filepath.Glob(filepath.Join(p.dir(owner), "*.json"))
	if err != nil {
		return nil, err
	}
	var out []meta
	for _, f := range files {
		raw, err := os.ReadFile(f)
		if err != nil {
			continue
		}
		var m meta
		if json.Unmarshal(raw, &m) != nil {
			continue
		}
		if m2, ok, _ := p.loadLocked(owner, m.Key); ok {
			out = append(out, m2)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Key < out[j].Key })
	return out, nil
}

func (p *Provider) sign(owner, key, method string, expires int64) string {
	mac := hmac.New(sha256.New, p.opts.SigningKey)
	fmt.Fprintf(mac, "%s\n%s\n%s\n%d", owner, key, method, expires)
	return hex.EncodeToString(mac.Sum(nil))
}

func (p *Provider) verify(raw, method string) (string, string, error) {
	denied := providererr.New(providererr.Denied, "invalid_url", "临时地址无效")
	u, err := url.Parse(raw)
	if err != nil || u.Scheme != "devobj" {
		return "", "", denied
	}
	keyBytes, err := base64.URLEncoding.DecodeString(strings.TrimPrefix(u.Path, "/"))
	if err != nil {
		return "", "", denied
	}
	q := u.Query()
	expires, err := strconv.ParseInt(q.Get("expires"), 10, 64)
	if err != nil || q.Get("method") != method {
		return "", "", denied
	}
	owner, key := u.Host, string(keyBytes)
	if !hmac.Equal([]byte(q.Get("sig")), []byte(p.sign(owner, key, method, expires))) {
		return "", "", denied
	}
	if expires <= p.clock.Now().Unix() {
		return "", "", providererr.New(providererr.Denied, "expired_url", "临时地址已过期")
	}
	return owner, key, nil
}

// FetchURL 用 GET 临时地址取对象（开发专用）。
func (p *Provider) FetchURL(_ context.Context, raw string) ([]byte, error) {
	owner, key, err := p.verify(raw, "GET")
	if err != nil {
		return nil, err
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	d, err := p.readLocked(owner, key)
	return d.Data, err
}

// UploadURL 用 PUT 临时地址上传（开发专用）。
func (p *Provider) UploadURL(_ context.Context, raw string, data []byte) error {
	owner, key, err := p.verify(raw, "PUT")
	if err != nil {
		return err
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	_, err = p.writeLocked(owner, key, data, objectstore.PutOptions{})
	return err
}

// RunLifecycle 立即执行一次生命周期清理（开发专用）。
func (p *Provider) RunLifecycle(_ context.Context) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	entries, err := os.ReadDir(p.opts.Root)
	if errors.Is(err, fs.ErrNotExist) {
		return nil
	}
	for _, e := range entries {
		if e.IsDir() {
			if _, err := p.allLocked(e.Name()); err != nil {
				return err
			}
		}
	}
	return nil
}

type store struct {
	p     *Provider
	owner string
}

func (s *store) enter(op string) error {
	if err := s.p.faults.Check(op); err != nil {
		return err
	}
	s.p.mu.Lock()
	return nil
}

func (s *store) URI(key string) string { return "obj://" + s.owner + "/" + key }

func (s *store) KeyOf(uri string) (string, error) { return KeyOfURI(uri, s.owner) }

// KeyOfURI 解析 obj://<作用域>/<键>：格式不对 Invalid，属于别的作用域（不是 owner）Denied。各实现共用。
func KeyOfURI(uri, owner string) (string, error) {
	m := uriRe.FindStringSubmatch(uri)
	if m == nil {
		return "", providererr.New(providererr.Invalid, "invalid_uri", "引用必须形如 obj://<租户>/<键>")
	}
	scope, key := m[1], m[2]
	if scope != devutil.PlatformID && !slug.MatchString(scope) {
		return "", providererr.New(providererr.Invalid, "invalid_uri", "引用里的作用域格式不对")
	}
	if err := CheckKey(key); err != nil {
		return "", err
	}
	if scope != owner {
		return "", providererr.New(providererr.Denied, "cross_scope_reference", "引用属于别的作用域")
	}
	return key, nil
}

func (s *store) Put(_ context.Context, key string, data []byte, opts objectstore.PutOptions) (objectstore.ObjectInfo, error) {
	if err := CheckKey(key); err != nil {
		return objectstore.ObjectInfo{}, err
	}
	if err := s.enter("Put"); err != nil {
		return objectstore.ObjectInfo{}, err
	}
	defer s.p.mu.Unlock()
	return s.p.writeLocked(s.owner, key, data, opts)
}

func (s *store) Get(_ context.Context, key string) (objectstore.ObjectData, error) {
	if err := CheckKey(key); err != nil {
		return objectstore.ObjectData{}, err
	}
	if err := s.enter("Get"); err != nil {
		return objectstore.ObjectData{}, err
	}
	defer s.p.mu.Unlock()
	return s.p.readLocked(s.owner, key)
}

func (s *store) Head(_ context.Context, key string) (objectstore.ObjectInfo, error) {
	if err := CheckKey(key); err != nil {
		return objectstore.ObjectInfo{}, err
	}
	if err := s.enter("Head"); err != nil {
		return objectstore.ObjectInfo{}, err
	}
	defer s.p.mu.Unlock()
	m, ok, err := s.p.loadLocked(s.owner, key)
	if err != nil {
		return objectstore.ObjectInfo{}, err
	}
	if !ok {
		return objectstore.ObjectInfo{}, providererr.New(providererr.NotFound, "not_found", "对象不存在")
	}
	return s.p.info(s.owner, m), nil
}

func (s *store) Delete(_ context.Context, key string) (bool, error) {
	if err := CheckKey(key); err != nil {
		return false, err
	}
	if err := s.enter("Delete"); err != nil {
		return false, err
	}
	defer s.p.mu.Unlock()
	_, ok, err := s.p.loadLocked(s.owner, key)
	if err != nil || !ok {
		return false, err
	}
	dataPath, metaPath := s.p.paths(s.owner, key)
	_ = os.Remove(metaPath)
	_ = os.Remove(dataPath)
	return true, nil
}

func (s *store) List(_ context.Context, opts objectstore.ListOptions) (objectstore.ObjectPage, error) {
	if err := s.enter("List"); err != nil {
		return objectstore.ObjectPage{}, err
	}
	defer s.p.mu.Unlock()
	limit := opts.Limit
	if limit == 0 {
		limit = 1000
	}
	if limit < 0 {
		return objectstore.ObjectPage{}, providererr.New(providererr.Invalid, "invalid_argument", "Limit 不能为负")
	}
	after := ""
	if opts.Cursor != "" {
		b, err := base64.URLEncoding.DecodeString(opts.Cursor)
		if err != nil {
			return objectstore.ObjectPage{}, providererr.New(providererr.Invalid, "invalid_cursor", "游标无效")
		}
		after = string(b)
	}
	all, err := s.p.allLocked(s.owner)
	if err != nil {
		return objectstore.ObjectPage{}, providererr.Wrap(providererr.Transient, "io_error", "列出对象失败", err)
	}
	var page objectstore.ObjectPage
	matched := 0
	for _, m := range all {
		if !strings.HasPrefix(m.Key, opts.Prefix) || (after != "" && m.Key <= after) {
			continue
		}
		matched++
		if len(page.Items) < limit {
			page.Items = append(page.Items, s.p.info(s.owner, m))
		}
	}
	if matched > limit {
		page.NextCursor = base64.URLEncoding.EncodeToString([]byte(page.Items[len(page.Items)-1].Key))
	}
	return page, nil
}

func (s *store) TemporaryURL(_ context.Context, key string, ttl time.Duration, method string) (string, error) {
	if err := CheckKey(key); err != nil {
		return "", err
	}
	if err := devutil.CheckPositive(ttl, "ttl"); err != nil {
		return "", err
	}
	if method != "GET" && method != "PUT" {
		return "", providererr.New(providererr.Invalid, "invalid_argument", "method 只能是 GET 或 PUT")
	}
	if err := s.p.faults.Check("TemporaryURL"); err != nil {
		return "", err
	}
	expires := s.p.clock.Now().Add(ttl).Unix()
	return fmt.Sprintf("devobj://%s/%s?method=%s&expires=%d&sig=%s",
		s.owner, base64.URLEncoding.EncodeToString([]byte(key)), method, expires, s.p.sign(s.owner, key, method, expires)), nil
}

func (s *store) SetLifecycle(_ context.Context, rules []objectstore.LifecycleRule) error {
	for _, r := range rules {
		if err := devutil.CheckPositive(r.ExpireAfter, "ExpireAfter"); err != nil {
			return err
		}
	}
	if err := s.enter("SetLifecycle"); err != nil {
		return err
	}
	defer s.p.mu.Unlock()
	s.p.rules[s.owner] = append([]objectstore.LifecycleRule(nil), rules...)
	return nil
}
