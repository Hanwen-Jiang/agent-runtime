// Package mirror 是包镜像的 HTTP 服务（设计书改动稿 W7；docs/plan/cloud-deploy.md D-15）：沙箱装依赖用的本地源，
// 按需从官方源拉取并缓存，协议与官方源相同，项目不用改：
//
//	/pypi/simple/<项目>/              PyPI 的 simple 索引（PEP 691 JSON；客户端不要 JSON 时给 PEP 503 的 HTML）
//	/pypi/files/<路径>                 包文件（files.pythonhosted.org 的同一条路径）
//	/npm/<包>                          npm 的精简包信息（application/vnd.npm.install-v1+json）
//	/npm/<包>/-/<文件>.tgz             npm 的包文件
//	/go/<模块>/@v/list、@latest、@v/<版本>.info|.mod|.zip   GOPROXY 协议
//	/go/sumdb/sum.golang.org/...       Go 的校验数据库（代理给 sum.golang.org）
//
// 认证：沙箱环境的短期凭据（libs/go-core/envcred，用途 mirror），放在路径前缀 /t/<凭据>/ 里（pip、npm、go 都能用，
// 不用另配认证），或 Authorization: Basic（口令是凭据）/ Bearer。凭据解出租户；请求里写的租户不作数。元数据里的下载地址
// 改写成指向镜像（带上同样的前缀），所以包文件的请求也带着凭据。日志里只记去掉凭据的路径。
//
// 上游只有构造时给的那几个（main 里写死官方源）。同一个文件的并发请求只向上游拉一次。包文件按上游给的哈希核对
// （PyPI 的 sha256、npm 的 integrity），对不上不缓存、不返回（502，客户端会重试）。元数据过期后先给旧的、在后台刷新，
// 上游不可用时继续给旧的。
//
// 按租户的限制（W7）：存储不按租户切；每个租户每天从上游拉的字节数（只算未命中）不超过凭据里的 mirror_miss_bytes
// 时照常缓存，超了只转发不缓存，到两倍时拒绝（429）。命中不限。
//
// 拉黑（Denylist）：某个版本被曝恶意时按“生态 + 包名 + 版本”清除并拉黑，立即对所有租户生效：索引里不再列它，
// 请求它的文件 404。
package mirror

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"html"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"path"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/envcred"
	"github.com/agentruntime/agent-runtime/services/go-pkg-mirror/internal/cache"
	"github.com/agentruntime/agent-runtime/services/go-pkg-mirror/internal/usage"
)

// Upstreams 是各生态的上游。
type Upstreams struct {
	PyPISimple string
	PyPIFiles  string
	NPM        string
	GoProxy    string
	SumDB      string
}

// Official 是写死的官方源（W7：上游只有官方源；私有源第一期不支持）。
func Official() Upstreams {
	return Upstreams{
		PyPISimple: "https://pypi.org/simple",
		PyPIFiles:  "https://files.pythonhosted.org",
		NPM:        "https://registry.npmjs.org",
		GoProxy:    "https://proxy.golang.org",
		SumDB:      "https://sum.golang.org",
	}
}

// LimitKey 是凭据里包镜像未命中字节数的限额名。
const LimitKey = "mirror_miss_bytes"

// Config 是服务的配置。
type Config struct {
	Blobs         *cache.Blobs
	Meta          *cache.Meta
	Upstreams     Upstreams
	Client        *http.Client
	CredentialKey []byte
	Usage         usage.Recorder
	Deny          *Denylist
	Log           *slog.Logger
	Clock         func() time.Time
	// MetaTTL 是索引与包信息的有效期（5 到 30 分钟），SumDBTTL 是校验数据库应答的。
	MetaTTL  time.Duration
	SumDBTTL time.Duration
}

// Server 是包镜像。
type Server struct {
	cfg      Config
	flights  sync.Map // 上游地址 → *flight
	pypiFile sync.Map // files 的路径 → fileInfo
	npmFile  sync.Map // <包>/<文件> → fileInfo
	refresh  sync.Map // 元数据的键 → struct{}（后台刷新中）
	Metrics  *Metrics
}

type fileInfo struct {
	expect  cache.Expect
	name    string
	version string
}

type flight struct {
	done chan struct{}
	err  error
}

// New 建一个包镜像。
func New(cfg Config) *Server {
	if cfg.Client == nil {
		cfg.Client = &http.Client{Timeout: 10 * time.Minute, Transport: &http.Transport{Proxy: nil, ResponseHeaderTimeout: time.Minute}}
	}
	if cfg.Clock == nil {
		cfg.Clock = time.Now
	}
	if cfg.MetaTTL <= 0 {
		cfg.MetaTTL = 10 * time.Minute
	}
	if cfg.SumDBTTL <= 0 {
		cfg.SumDBTTL = 5 * time.Minute
	}
	if cfg.Log == nil {
		cfg.Log = slog.New(slog.NewJSONHandler(io.Discard, nil))
	}
	if cfg.Deny == nil {
		cfg.Deny = NewDenylist("")
	}
	if cfg.Usage == nil {
		cfg.Usage = usage.New(nil)
	}
	return &Server{cfg: cfg, Metrics: NewMetrics()}
}

type request struct {
	w      http.ResponseWriter
	r      *http.Request
	claims envcred.Claims
	base   string // 改写下载地址用的根（含 /t/<凭据> 前缀）
	rest   string // 去掉前缀后的路径
}

func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path == "/healthz" {
		_, _ = io.WriteString(w, "ok")
		return
	}
	req, ok := s.authenticate(w, r)
	if !ok {
		return
	}
	if r.Method != http.MethodGet && r.Method != http.MethodHead {
		problem(w, http.StatusMethodNotAllowed, "method_not_allowed", "包镜像只读")
		return
	}
	switch {
	case strings.HasPrefix(req.rest, "/pypi/simple/"):
		s.pypiIndex(req, strings.TrimPrefix(req.rest, "/pypi/simple/"))
	case strings.HasPrefix(req.rest, "/pypi/files/"):
		s.pypiFileGet(req, strings.TrimPrefix(req.rest, "/pypi/files/"))
	case strings.HasPrefix(req.rest, "/npm/"):
		s.npm(req, strings.TrimPrefix(req.rest, "/npm/"))
	case strings.HasPrefix(req.rest, "/go/"):
		s.goProxy(req, strings.TrimPrefix(req.rest, "/go/"))
	default:
		problem(w, http.StatusNotFound, "not_found", "没有这个路径")
	}
}

// authenticate 取出并校验凭据：路径前缀 /t/<凭据>/，或 Authorization。
func (s *Server) authenticate(w http.ResponseWriter, r *http.Request) (*request, bool) {
	p := r.URL.EscapedPath()
	token, prefix := "", ""
	if strings.HasPrefix(p, "/t/") {
		rest := strings.TrimPrefix(p, "/t/")
		i := strings.IndexByte(rest, '/')
		if i > 0 {
			token, prefix, p = rest[:i], "/t/"+rest[:i], rest[i:]
		}
	}
	if token == "" {
		auth := r.Header.Get("Authorization")
		if after, ok := strings.CutPrefix(auth, "Bearer "); ok {
			token = after
		} else if _, pass, ok := r.BasicAuth(); ok {
			token = pass
		}
	}
	claims, err := envcred.Verify(s.cfg.CredentialKey, token, envcred.PurposeMirror, s.cfg.Clock())
	if err != nil {
		s.Metrics.inc("unauthorized", "")
		problem(w, http.StatusUnauthorized, "invalid_credential", "包镜像要沙箱环境的凭据")
		return nil, false
	}
	unescaped, err := url.PathUnescape(p)
	if err != nil {
		problem(w, http.StatusBadRequest, "bad_path", "路径不合法")
		return nil, false
	}
	scheme := "http"
	if r.TLS != nil || strings.EqualFold(r.Header.Get("X-Forwarded-Proto"), "https") {
		scheme = "https"
	}
	return &request{w: w, r: r, claims: claims, base: scheme + "://" + r.Host + prefix, rest: unescaped}, true
}

// --- PyPI ---

var pypiNormalize = regexp.MustCompile(`[-_.]+`)

func normalizePyPI(name string) string { return strings.ToLower(pypiNormalize.ReplaceAllString(name, "-")) }

type pypiIndex struct {
	Meta     map[string]any   `json:"meta"`
	Name     string           `json:"name"`
	Files    []map[string]any `json:"files"`
	Versions []string         `json:"versions,omitempty"`
}

func (s *Server) pypiIndex(req *request, rest string) {
	project := normalizePyPI(strings.Trim(rest, "/"))
	if project == "" || strings.Contains(project, "/") {
		problem(req.w, http.StatusNotFound, "not_found", "只代理单个项目的索引")
		return
	}
	body, err := s.metadata(req, "pypi:index:"+project, s.cfg.Upstreams.PyPISimple+"/"+project+"/",
		"application/vnd.pypi.simple.v1+json", s.cfg.MetaTTL, "pypi")
	if err != nil {
		s.upstreamError(req, err)
		return
	}
	var idx pypiIndex
	if err := json.Unmarshal(body, &idx); err != nil {
		problem(req.w, http.StatusBadGateway, "upstream_bad_response", "上游的索引格式不对")
		return
	}
	files := idx.Files[:0]
	for _, f := range idx.Files {
		filename, _ := f["filename"].(string)
		raw, _ := f["url"].(string)
		version := pypiVersion(project, filename)
		if s.cfg.Deny.Contains("pypi", project, version) {
			continue
		}
		rel, ok := strings.CutPrefix(raw, s.cfg.Upstreams.PyPIFiles+"/")
		if !ok {
			continue // 不在官方的文件站上的链接不给
		}
		var expect cache.Expect
		if hashes, ok := f["hashes"].(map[string]any); ok {
			expect.SHA256, _ = hashes["sha256"].(string)
		}
		s.pypiFile.Store(rel, fileInfo{expect: expect, name: project, version: version})
		f["url"] = req.base + "/pypi/files/" + rel
		// 不给单独的元数据文件（PEP 658）：pip 直接下载整个包，镜像少一类要核对的文件。
		delete(f, "core-metadata")
		delete(f, "data-dist-info-metadata")
		delete(f, "dist-info-metadata")
		files = append(files, f)
	}
	idx.Files = files
	if strings.Contains(req.r.Header.Get("Accept"), "application/vnd.pypi.simple.v1+json") {
		req.w.Header().Set("Content-Type", "application/vnd.pypi.simple.v1+json")
		_ = json.NewEncoder(req.w).Encode(idx)
		return
	}
	req.w.Header().Set("Content-Type", "text/html; charset=utf-8")
	var b strings.Builder
	b.WriteString("<!DOCTYPE html>\n<html><head><meta name=\"pypi:repository-version\" content=\"1.1\"></head><body>\n")
	for _, f := range idx.Files {
		link, _ := f["url"].(string)
		if hashes, ok := f["hashes"].(map[string]any); ok {
			if h, ok := hashes["sha256"].(string); ok {
				link += "#sha256=" + h
			}
		}
		attrs := ""
		if rp, ok := f["requires-python"].(string); ok && rp != "" {
			attrs += ` data-requires-python="` + html.EscapeString(rp) + `"`
		}
		if y, ok := f["yanked"]; ok && y != false && y != nil {
			attrs += ` data-yanked=""`
		}
		name, _ := f["filename"].(string)
		fmt.Fprintf(&b, "<a href=\"%s\"%s>%s</a><br>\n", html.EscapeString(link), attrs, html.EscapeString(name))
	}
	b.WriteString("</body></html>\n")
	_, _ = io.WriteString(req.w, b.String())
}

// pypiVersion 从文件名取版本：wheel 是 <名>-<版本>-…whl，sdist 是 <名>-<版本>.tar.gz 或 .zip。
func pypiVersion(project, filename string) string {
	base := filename
	for _, ext := range []string{".tar.gz", ".tar.bz2", ".zip", ".whl"} {
		if strings.HasSuffix(base, ext) {
			base = strings.TrimSuffix(base, ext)
			break
		}
	}
	parts := strings.Split(base, "-")
	if strings.HasSuffix(filename, ".whl") && len(parts) >= 2 {
		return parts[1]
	}
	// sdist：名字里可能有 -，版本是最后一段。
	if len(parts) >= 2 {
		return parts[len(parts)-1]
	}
	return ""
}

func (s *Server) pypiFileGet(req *request, rel string) {
	rel = strings.TrimPrefix(path.Clean("/"+rel), "/")
	filename := path.Base(rel)
	v, ok := s.pypiFile.Load(rel)
	if !ok {
		// 没经过索引就来要文件：按文件名推出项目，先取一遍索引（拿到上游给的哈希）。
		project := normalizePyPI(strings.SplitN(filename, "-", 2)[0])
		if _, err := s.metadata(req, "pypi:index:"+project, s.cfg.Upstreams.PyPISimple+"/"+project+"/",
			"application/vnd.pypi.simple.v1+json", s.cfg.MetaTTL, "pypi"); err == nil {
			s.reindexPyPI(project)
		}
		v, ok = s.pypiFile.Load(rel)
	}
	info := fileInfo{name: normalizePyPI(strings.SplitN(filename, "-", 2)[0])}
	if ok {
		info = v.(fileInfo)
	}
	if info.version == "" {
		info.version = pypiVersion(info.name, filename)
	}
	if s.cfg.Deny.Contains("pypi", info.name, info.version) {
		problem(req.w, http.StatusNotFound, "denied_version", "这个版本被拉黑了")
		return
	}
	upstream := s.cfg.Upstreams.PyPIFiles + "/" + rel
	s.serveBlob(req, "pypi", cache.Origin{Ecosystem: "pypi", Name: info.name, Version: info.version, URL: upstream}, info.expect, contentTypeOf(filename))
}

// reindexPyPI 从缓存的索引里补上每个文件的哈希（不改写地址，只填 pypiFile）。
func (s *Server) reindexPyPI(project string) {
	e, _, ok := s.cfg.Meta.Get("pypi:index:"+project, time.Hour, s.cfg.Clock())
	if !ok {
		return
	}
	var idx pypiIndex
	if json.Unmarshal(e.Body, &idx) != nil {
		return
	}
	for _, f := range idx.Files {
		filename, _ := f["filename"].(string)
		raw, _ := f["url"].(string)
		rel, ok := strings.CutPrefix(raw, s.cfg.Upstreams.PyPIFiles+"/")
		if !ok {
			continue
		}
		var expect cache.Expect
		if hashes, ok := f["hashes"].(map[string]any); ok {
			expect.SHA256, _ = hashes["sha256"].(string)
		}
		s.pypiFile.Store(rel, fileInfo{expect: expect, name: project, version: pypiVersion(project, filename)})
	}
}

// --- npm ---

func (s *Server) npm(req *request, rest string) {
	if i := strings.Index(rest, "/-/"); i > 0 {
		s.npmTarball(req, rest[:i], rest[i+3:])
		return
	}
	name := strings.Trim(rest, "/")
	if name == "" || (strings.Count(name, "/") > 1) || (strings.Contains(name, "/") && !strings.HasPrefix(name, "@")) {
		problem(req.w, http.StatusNotFound, "not_found", "只代理单个包的信息")
		return
	}
	doc, err := s.npmPackument(req, name)
	if err != nil {
		s.upstreamError(req, err)
		return
	}
	versions, _ := doc["versions"].(map[string]any)
	for ver, raw := range versions {
		v, _ := raw.(map[string]any)
		if s.cfg.Deny.Contains("npm", name, ver) || v == nil {
			delete(versions, ver)
			continue
		}
		dist, _ := v["dist"].(map[string]any)
		if dist == nil {
			continue
		}
		tarball, _ := dist["tarball"].(string)
		file := path.Base(tarball)
		dist["tarball"] = req.base + "/npm/" + name + "/-/" + file
	}
	if tags, ok := doc["dist-tags"].(map[string]any); ok {
		for tag, v := range tags {
			if ver, _ := v.(string); versions[ver] == nil {
				delete(tags, tag)
			}
		}
		if _, ok := tags["latest"]; !ok && len(versions) > 0 {
			tags["latest"] = highestVersion(versions)
		}
	}
	req.w.Header().Set("Content-Type", "application/vnd.npm.install-v1+json")
	_ = json.NewEncoder(req.w).Encode(doc)
}

// npmPackument 取精简的包信息，并记下每个包文件的 integrity。
func (s *Server) npmPackument(req *request, name string) (map[string]any, error) {
	escaped := strings.Replace(name, "/", "%2f", 1)
	body, err := s.metadata(req, "npm:"+name, s.cfg.Upstreams.NPM+"/"+escaped,
		"application/vnd.npm.install-v1+json; q=1.0, application/json; q=0.8", s.cfg.MetaTTL, "npm")
	if err != nil {
		return nil, err
	}
	var doc map[string]any
	if err := json.Unmarshal(body, &doc); err != nil {
		return nil, errUpstreamFormat
	}
	versions, _ := doc["versions"].(map[string]any)
	for ver, raw := range versions {
		v, _ := raw.(map[string]any)
		dist, _ := v["dist"].(map[string]any)
		if dist == nil {
			continue
		}
		tarball, _ := dist["tarball"].(string)
		integrity, _ := dist["integrity"].(string)
		s.npmFile.Store(name+"/"+path.Base(tarball), fileInfo{expect: cache.Expect{Integrity: integrity}, name: name, version: ver})
	}
	return doc, nil
}

func (s *Server) npmTarball(req *request, name, file string) {
	file = path.Base(file)
	v, ok := s.npmFile.Load(name + "/" + file)
	if !ok {
		if _, err := s.npmPackument(req, name); err == nil {
			v, ok = s.npmFile.Load(name + "/" + file)
		}
	}
	if !ok {
		problem(req.w, http.StatusNotFound, "not_found", "包信息里没有这个文件")
		return
	}
	info := v.(fileInfo)
	if s.cfg.Deny.Contains("npm", name, info.version) {
		problem(req.w, http.StatusNotFound, "denied_version", "这个版本被拉黑了")
		return
	}
	upstream := s.cfg.Upstreams.NPM + "/" + name + "/-/" + file
	s.serveBlob(req, "npm", cache.Origin{Ecosystem: "npm", Name: name, Version: info.version, URL: upstream}, info.expect, "application/octet-stream")
}

// highestVersion 是语义化版本里最大的（预发布版排在正式版后面不算；比不出来时按字符串）。
func highestVersion(versions map[string]any) string {
	keys := make([]string, 0, len(versions))
	for k := range versions {
		if !strings.Contains(k, "-") {
			keys = append(keys, k)
		}
	}
	if len(keys) == 0 {
		for k := range versions {
			keys = append(keys, k)
		}
	}
	sort.Slice(keys, func(i, j int) bool { return compareSemver(keys[i], keys[j]) < 0 })
	return keys[len(keys)-1]
}

func compareSemver(a, b string) int {
	pa, pb := strings.SplitN(strings.SplitN(a, "-", 2)[0], ".", 3), strings.SplitN(strings.SplitN(b, "-", 2)[0], ".", 3)
	for i := 0; i < 3; i++ {
		var x, y int
		if i < len(pa) {
			x, _ = strconv.Atoi(pa[i])
		}
		if i < len(pb) {
			y, _ = strconv.Atoi(pb[i])
		}
		if x != y {
			if x < y {
				return -1
			}
			return 1
		}
	}
	return strings.Compare(a, b)
}

// --- Go ---

func (s *Server) goProxy(req *request, rest string) {
	if strings.HasPrefix(rest, "sumdb/") {
		s.sumdb(req, strings.TrimPrefix(rest, "sumdb/"))
		return
	}
	module, tail, ok := strings.Cut(rest, "/@")
	if !ok || module == "" {
		problem(req.w, http.StatusNotFound, "not_found", "不是 GOPROXY 协议的路径")
		return
	}
	name := unescapeGo(module)
	upstream := s.cfg.Upstreams.GoProxy + "/" + module + "/@" + tail
	switch {
	case tail == "v/list":
		body, err := s.metadata(req, "go:list:"+module, upstream, "", s.cfg.MetaTTL, "go")
		if err != nil {
			s.upstreamError(req, err)
			return
		}
		var keep []string
		for _, line := range strings.Split(string(body), "\n") {
			if line = strings.TrimSpace(line); line != "" && !s.cfg.Deny.Contains("go", name, line) {
				keep = append(keep, line)
			}
		}
		req.w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		_, _ = io.WriteString(req.w, strings.Join(keep, "\n")+"\n")
	case tail == "latest":
		body, err := s.metadata(req, "go:latest:"+module, upstream, "", s.cfg.MetaTTL, "go")
		if err != nil {
			s.upstreamError(req, err)
			return
		}
		var info struct{ Version string }
		if json.Unmarshal(body, &info) == nil && s.cfg.Deny.Contains("go", name, info.Version) {
			problem(req.w, http.StatusNotFound, "denied_version", "这个版本被拉黑了")
			return
		}
		req.w.Header().Set("Content-Type", "application/json")
		_, _ = req.w.Write(body)
	case strings.HasPrefix(tail, "v/"):
		file := strings.TrimPrefix(tail, "v/")
		ext := path.Ext(file)
		if ext != ".info" && ext != ".mod" && ext != ".zip" {
			problem(req.w, http.StatusNotFound, "not_found", "不是 GOPROXY 协议的文件")
			return
		}
		version := unescapeGo(strings.TrimSuffix(file, ext))
		if s.cfg.Deny.Contains("go", name, version) {
			problem(req.w, http.StatusGone, "denied_version", "这个版本被拉黑了")
			return
		}
		ct := map[string]string{".info": "application/json", ".mod": "text/plain; charset=utf-8", ".zip": "application/zip"}[ext]
		s.serveBlob(req, "go", cache.Origin{Ecosystem: "go", Name: name, Version: version, URL: upstream}, cache.Expect{}, ct)
	default:
		problem(req.w, http.StatusNotFound, "not_found", "不是 GOPROXY 协议的路径")
	}
}

// unescapeGo 把 GOPROXY 协议的大小写转义（!a 是 A）还原。
func unescapeGo(s string) string {
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		if s[i] == '!' && i+1 < len(s) {
			b.WriteByte(s[i+1] - 'a' + 'A')
			i++
			continue
		}
		b.WriteByte(s[i])
	}
	return b.String()
}

func (s *Server) sumdb(req *request, rest string) {
	db, tail, _ := strings.Cut(rest, "/")
	if db != "sum.golang.org" {
		problem(req.w, http.StatusNotFound, "not_found", "只代理 sum.golang.org")
		return
	}
	if tail == "supported" {
		req.w.WriteHeader(http.StatusOK)
		return
	}
	body, err := s.metadata(req, "go:sumdb:"+tail, s.cfg.Upstreams.SumDB+"/"+tail, "", s.cfg.SumDBTTL, "go")
	if err != nil {
		s.upstreamError(req, err)
		return
	}
	req.w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	_, _ = req.w.Write(body)
}

// --- 元数据 ---

var errUpstreamFormat = errors.New("上游返回的格式不对")

type upstreamStatus struct{ code int }

func (e upstreamStatus) Error() string { return fmt.Sprintf("上游返回 HTTP %d", e.code) }

// metadata 取一份元数据：新鲜的直接给；过期的先给旧的、后台刷新；没有的去上游拉（拉的字节算作这个租户的未命中）。
// 上游不可用时有旧的就给旧的。
func (s *Server) metadata(req *request, key, upstream, accept string, ttl time.Duration, eco string) ([]byte, error) {
	now := s.cfg.Clock()
	e, fresh, ok := s.cfg.Meta.Get(key, ttl, now)
	if ok && fresh {
		s.Metrics.inc("meta_hit", eco)
		return e.Body, nil
	}
	if ok {
		s.Metrics.inc("meta_stale", eco)
		if _, busy := s.refresh.LoadOrStore(key, struct{}{}); !busy {
			tenant := req.claims.TenantID
			go func() {
				defer s.refresh.Delete(key)
				ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
				defer cancel()
				_, _ = s.fetchMeta(ctx, key, upstream, accept, tenant)
			}()
		}
		return e.Body, nil
	}
	if err := s.quotaAllows(req, eco); err != nil {
		return nil, err
	}
	s.Metrics.inc("meta_miss", eco)
	body, err := s.fetchMeta(req.r.Context(), key, upstream, accept, req.claims.TenantID)
	if err != nil {
		return nil, err
	}
	return body, nil
}

func (s *Server) fetchMeta(ctx context.Context, key, upstream, accept, tenant string) ([]byte, error) {
	hreq, err := http.NewRequestWithContext(ctx, http.MethodGet, upstream, nil)
	if err != nil {
		return nil, err
	}
	if accept != "" {
		hreq.Header.Set("Accept", accept)
	}
	hreq.Header.Set("User-Agent", "agentruntime-pkg-mirror/0.1")
	res, err := s.cfg.Client.Do(hreq)
	if err != nil {
		return nil, err
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		return nil, upstreamStatus{res.StatusCode}
	}
	body, err := io.ReadAll(io.LimitReader(res.Body, 64<<20))
	if err != nil {
		return nil, err
	}
	s.cfg.Usage.Add(tenant, int64(len(body)), s.cfg.Clock())
	s.Metrics.add("upstream_bytes", "meta", int64(len(body)))
	if err := s.cfg.Meta.Put(key, body, res.Header.Get("Content-Type"), s.cfg.Clock()); err != nil {
		s.cfg.Log.Warn("元数据写不进缓存", "key", key, "err", err.Error())
	}
	return body, nil
}

// --- 包文件 ---

type mode int

const (
	modeCache mode = iota
	modePassthrough
)

var errQuota = errors.New("quota")

// quotaAllows 判断这个租户还能不能从上游拉（未命中的配额）：超了一倍只转发不缓存，到两倍拒绝。
func (s *Server) quotaAllows(req *request, eco string) error {
	if s.missMode(req) < 0 {
		s.Metrics.inc("quota_denied", eco)
		return errQuota
	}
	return nil
}

// missMode：-1 拒绝，modeCache 照常，modePassthrough 只转发。
func (s *Server) missMode(req *request) mode {
	limit := req.claims.Limits[LimitKey]
	if limit <= 0 {
		return modeCache
	}
	used := s.cfg.Usage.Used(req.r.Context(), req.claims.TenantID, s.cfg.Clock())
	switch {
	case used < limit:
		return modeCache
	case used < 2*limit:
		return modePassthrough
	default:
		return -1
	}
}

// serveBlob：命中就从库里给（核对内容）；没命中就从上游拉：同一个地址同时只拉一次；核对上游给的哈希；按配额决定进不进库。
func (s *Server) serveBlob(req *request, eco string, origin cache.Origin, expect cache.Expect, contentType string) {
	for attempt := 0; attempt < 2; attempt++ {
		if key, ok := s.cfg.Blobs.Lookup(origin.URL); ok {
			r, err := s.cfg.Blobs.Get(key)
			if err == nil {
				defer r.Close()
				s.Metrics.inc("hit", eco)
				req.w.Header().Set("Content-Type", contentType)
				req.w.Header().Set("Content-Length", strconv.FormatInt(r.Size, 10))
				if req.r.Method == http.MethodHead {
					return
				}
				_, _ = io.Copy(req.w, r)
				return
			}
			// 被改过（已删、已告警）：当作没命中，从上游重新拉。
		}
		m := s.missMode(req)
		if m < 0 {
			s.Metrics.inc("quota_denied", eco)
			problem(req.w, http.StatusTooManyRequests, "mirror_quota_exceeded", "今天从上游拉依赖的流量到了上限")
			return
		}
		if m == modePassthrough {
			s.Metrics.inc("passthrough", eco)
			s.fetchBlob(req, eco, origin, expect, contentType, false)
			return
		}
		// 同一个地址同时只拉一次：别的请求等它拉完再从库里取。
		f := &flight{done: make(chan struct{})}
		if existing, loaded := s.flights.LoadOrStore(origin.URL, f); loaded {
			other := existing.(*flight)
			select {
			case <-other.done:
			case <-req.r.Context().Done():
				return
			}
			if other.err != nil && !errors.Is(other.err, errServed) {
				s.upstreamError(req, other.err)
				return
			}
			continue
		}
		s.Metrics.inc("miss", eco)
		f.err = s.fetchBlob(req, eco, origin, expect, contentType, true)
		s.flights.Delete(origin.URL)
		close(f.done)
		return
	}
	problem(req.w, http.StatusBadGateway, "mirror_unavailable", "包文件暂时拿不到，稍后重试")
}

// errServed 表示文件已经交给了发起拉取的那个请求（别的等待者再从库里取）。
var errServed = errors.New("served")

// fetchBlob 从上游拉一个包文件并交给客户端。store 为真时核对后进库；为假时核对后只转发。超过大小上限的只转发（不核对，
// 客户端自己核对）。返回给等待者看的错误。
func (s *Server) fetchBlob(req *request, eco string, origin cache.Origin, expect cache.Expect, contentType string, store bool) error {
	hreq, err := http.NewRequestWithContext(req.r.Context(), http.MethodGet, origin.URL, nil)
	if err != nil {
		problem(req.w, http.StatusBadGateway, "mirror_unavailable", "上游地址不对")
		return err
	}
	hreq.Header.Set("User-Agent", "agentruntime-pkg-mirror/0.1")
	res, err := s.cfg.Client.Do(hreq)
	if err != nil {
		s.upstreamError(req, err)
		return err
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		err := upstreamStatus{res.StatusCode}
		s.upstreamError(req, err)
		return err
	}
	tenant := req.claims.TenantID
	counting := &countingReader{r: res.Body}
	defer func() {
		s.cfg.Usage.Add(tenant, counting.n, s.cfg.Clock())
		s.Metrics.add("upstream_bytes", eco, counting.n)
	}()
	if res.ContentLength > s.cfg.Blobs.MaxFile() {
		s.Metrics.inc("oversize", eco)
		req.w.Header().Set("Content-Type", contentType)
		req.w.Header().Set("Content-Length", strconv.FormatInt(res.ContentLength, 10))
		_, _ = io.Copy(req.w, counting)
		return errServed
	}
	staged, err := s.cfg.Blobs.Stage(origin, expect)
	if err != nil {
		problem(req.w, http.StatusInternalServerError, "mirror_error", "镜像写不了临时文件")
		return err
	}
	if _, err := io.Copy(staged, counting); err != nil {
		if errors.Is(err, cache.ErrTooLarge) {
			// 长度事先不知道、拉到一半超了：已拉的部分加上剩下的原样转发，不进库。
			s.Metrics.inc("oversize", eco)
			tmp, _ := os.Open(staged.TempPath())
			req.w.Header().Set("Content-Type", contentType)
			if tmp != nil {
				_, _ = io.Copy(req.w, tmp)
				tmp.Close()
			}
			staged.Abort()
			_, _ = io.Copy(req.w, counting)
			return errServed
		}
		staged.Abort()
		s.upstreamError(req, err)
		return err
	}
	key, err := staged.Commit(store)
	if err != nil {
		if errors.Is(err, cache.ErrMismatch) {
			problem(req.w, http.StatusBadGateway, "upstream_mismatch", "上游的文件与它的索引对不上，没有缓存，稍后重试")
			return err
		}
		problem(req.w, http.StatusInternalServerError, "mirror_error", "镜像写不进库")
		return err
	}
	if !store {
		defer staged.Discard()
		f, err := os.Open(staged.TempPath())
		if err != nil {
			problem(req.w, http.StatusInternalServerError, "mirror_error", "镜像读不了临时文件")
			return err
		}
		defer f.Close()
		info, _ := f.Stat()
		req.w.Header().Set("Content-Type", contentType)
		if info != nil {
			req.w.Header().Set("Content-Length", strconv.FormatInt(info.Size(), 10))
		}
		_, _ = io.Copy(req.w, f)
		return errServed
	}
	r, err := s.cfg.Blobs.Get(key)
	if err != nil {
		problem(req.w, http.StatusInternalServerError, "mirror_error", "镜像读不回刚进库的文件")
		return err
	}
	defer r.Close()
	req.w.Header().Set("Content-Type", contentType)
	req.w.Header().Set("Content-Length", strconv.FormatInt(r.Size, 10))
	_, _ = io.Copy(req.w, r)
	return errServed
}

type countingReader struct {
	r io.Reader
	n int64
}

func (c *countingReader) Read(p []byte) (int, error) {
	n, err := c.r.Read(p)
	atomic.AddInt64(&c.n, int64(n))
	return n, err
}

// --- 小工具 ---

func (s *Server) upstreamError(req *request, err error) {
	var st upstreamStatus
	switch {
	case errors.Is(err, errQuota):
		problem(req.w, http.StatusTooManyRequests, "mirror_quota_exceeded", "今天从上游拉依赖的流量到了上限")
	case errors.As(err, &st) && (st.code == http.StatusNotFound || st.code == http.StatusGone):
		problem(req.w, st.code, "not_found", "上游没有这个包或版本")
	default:
		s.cfg.Log.Warn("上游不可用", "path", req.rest, "err", err.Error())
		problem(req.w, http.StatusBadGateway, "upstream_unavailable", "上游暂时不可用，稍后重试")
	}
}

func problem(w http.ResponseWriter, status int, code, message string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]string{"code": code, "message": message})
}

func contentTypeOf(filename string) string {
	switch {
	case strings.HasSuffix(filename, ".whl"), strings.HasSuffix(filename, ".zip"):
		return "application/zip"
	case strings.HasSuffix(filename, ".tar.gz"):
		return "application/gzip"
	default:
		return "application/octet-stream"
	}
}

// SanitizePath 去掉路径里的凭据（记日志用）。
func SanitizePath(p string) string {
	if strings.HasPrefix(p, "/t/") {
		rest := strings.TrimPrefix(p, "/t/")
		if i := strings.IndexByte(rest, '/'); i >= 0 {
			return "/t/***" + rest[i:]
		}
		return "/t/***"
	}
	return p
}
