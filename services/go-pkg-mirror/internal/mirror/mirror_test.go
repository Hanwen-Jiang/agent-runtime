package mirror_test

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/envcred"
	"github.com/agentruntime/agent-runtime/services/go-pkg-mirror/internal/cache"
	"github.com/agentruntime/agent-runtime/services/go-pkg-mirror/internal/mirror"
	"github.com/agentruntime/agent-runtime/services/go-pkg-mirror/internal/usage"
)

var credKey = bytes.Repeat([]byte("k"), 32)

const adminToken = "admin-token-0123456789abcdef0123"

type fixture struct {
	t      *testing.T
	up     *upstream
	srv    *mirror.Server
	http   *httptest.Server
	admin  *httptest.Server
	blobs  *cache.Blobs
	dir    string
	usage  *usage.Tracker
	alerts []string
	mu     sync.Mutex
}

func newFixture(t *testing.T, edit func(*cache.Options)) *fixture {
	t.Helper()
	f := &fixture{t: t, up: newUpstream(t), usage: usage.New(nil)}
	dir := t.TempDir()
	opt := cache.Options{Dir: dir, Budget: 64 << 20, Alert: func(kind, detail string) {
		f.mu.Lock()
		f.alerts = append(f.alerts, kind)
		f.mu.Unlock()
	}}
	if edit != nil {
		edit(&opt)
	}
	blobs, err := cache.Open(opt)
	if err != nil {
		t.Fatal(err)
	}
	meta, err := cache.OpenMeta(dir, 8<<20)
	if err != nil {
		t.Fatal(err)
	}
	f.blobs = blobs
	f.dir = dir
	f.srv = mirror.New(mirror.Config{
		Blobs: blobs, Meta: meta, Upstreams: f.up.upstreams(), CredentialKey: credKey, Usage: f.usage,
		Deny: mirror.NewDenylist(filepath.Join(dir, "denylist.json")), MetaTTL: time.Minute,
	})
	f.http = httptest.NewServer(f.srv)
	f.admin = httptest.NewServer(f.srv.Admin(adminToken))
	t.Cleanup(f.http.Close)
	t.Cleanup(f.admin.Close)
	return f
}

func token(t *testing.T, tenant string, limit int64) string {
	t.Helper()
	c := envcred.Claims{EnvRef: "sbe-0123456789abcdef", TenantID: tenant, ExpiresAt: time.Now().Add(time.Hour).Unix(), Purpose: envcred.PurposeMirror}
	if limit > 0 {
		c.Limits = map[string]int64{mirror.LimitKey: limit}
	}
	tok, err := envcred.Issue(credKey, c)
	if err != nil {
		t.Fatal(err)
	}
	return tok
}

func (f *fixture) get(path string, header map[string]string) (int, []byte, http.Header) {
	f.t.Helper()
	req, _ := http.NewRequest(http.MethodGet, f.http.URL+path, nil)
	for k, v := range header {
		req.Header.Set(k, v)
	}
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		f.t.Fatal(err)
	}
	defer res.Body.Close()
	body, _ := io.ReadAll(res.Body)
	return res.StatusCode, body, res.Header
}

func (f *fixture) pypiJSON(prefix string) map[string]any {
	f.t.Helper()
	status, body, _ := f.get(prefix+"/pypi/simple/Demo_Pkg/", map[string]string{"Accept": "application/vnd.pypi.simple.v1+json"})
	if status != 200 {
		f.t.Fatalf("索引：%d %s", status, body)
	}
	var idx map[string]any
	_ = json.Unmarshal(body, &idx)
	return idx
}

func TestPyPIIndexIsRewrittenAndFilesAreCachedAndVerified(t *testing.T) {
	f := newFixture(t, nil)
	prefix := "/t/" + token(t, "t-a", 0)
	idx := f.pypiJSON(prefix)
	files := idx["files"].([]any)
	if len(files) != 2 {
		t.Fatalf("不在官方文件站上的链接不给：%v", files)
	}
	first := files[0].(map[string]any)
	link := first["url"].(string)
	if !strings.HasPrefix(link, f.http.URL+prefix+"/pypi/files/packages/aa/bb/") {
		t.Fatalf("下载地址改写成指向镜像、带着凭据前缀：%s", link)
	}
	if _, ok := first["core-metadata"]; ok {
		t.Fatal("不给单独的元数据文件")
	}
	_, html, hdr := f.get(prefix+"/pypi/simple/demo-pkg/", map[string]string{"Accept": "text/html"})
	if !strings.Contains(hdr.Get("Content-Type"), "text/html") || !strings.Contains(string(html), "#sha256="+hexSum(f.up.wheel)) {
		t.Fatalf("不要 JSON 的客户端给 HTML：%s", html)
	}
	path := strings.TrimPrefix(link, f.http.URL)
	for i := 0; i < 3; i++ {
		status, body, _ := f.get(path, nil)
		if status != 200 || !bytes.Equal(body, f.up.wheel) {
			t.Fatalf("第 %d 次下载：%d", i, status)
		}
	}
	if n := f.up.hit("/files/packages/aa/bb/demo_pkg-1.0.0-py3-none-any.whl"); n != 1 {
		t.Fatalf("同一个文件只向上游拉一次，实际 %d 次", n)
	}
	if f.srv.Metrics.Get("hit", "pypi") != 2 {
		t.Fatalf("命中计数：%d", f.srv.Metrics.Get("hit", "pypi"))
	}
}

func TestUpstreamHashMismatchIsNeitherCachedNorServed(t *testing.T) {
	f := newFixture(t, nil)
	f.up.badIndex = true
	prefix := "/t/" + token(t, "t-a", 0)
	idx := f.pypiJSON(prefix)
	link := idx["files"].([]any)[0].(map[string]any)["url"].(string)
	status, body, _ := f.get(strings.TrimPrefix(link, f.http.URL), nil)
	if status != http.StatusBadGateway || !strings.Contains(string(body), "upstream_mismatch") {
		t.Fatalf("哈希对不上：%d %s", status, body)
	}
	if f.blobs.Stats().Files != 0 || len(f.alerts) != 1 {
		t.Fatalf("不缓存、记告警：%d %v", f.blobs.Stats().Files, f.alerts)
	}
}

func TestTamperedCacheIsNotServedAndIsRefetched(t *testing.T) {
	f := newFixture(t, nil)
	prefix := "/t/" + token(t, "t-a", 0)
	link := f.pypiJSON(prefix)["files"].([]any)[0].(map[string]any)["url"].(string)
	path := strings.TrimPrefix(link, f.http.URL)
	f.get(path, nil)
	var stored string
	_ = filepath.Walk(filepath.Join(f.dir, "blobs"), func(p string, info os.FileInfo, err error) error {
		if err == nil && !info.IsDir() {
			stored = p
		}
		return nil
	})
	if stored == "" {
		t.Fatal("找不到库里的文件")
	}
	if err := os.WriteFile(stored, []byte("evil"), 0o644); err != nil {
		t.Fatal(err)
	}
	status, body, _ := f.get(path, nil)
	if status != 200 || !bytes.Equal(body, f.up.wheel) {
		t.Fatalf("客户端拿不到被改过的文件，拿到的是重新从上游拉的：%d", status)
	}
	if f.up.hit("/files/packages/aa/bb/demo_pkg-1.0.0-py3-none-any.whl") != 2 || len(f.alerts) == 0 || f.alerts[0] != "blob_corrupt" {
		t.Fatalf("记告警并重新拉：%v", f.alerts)
	}
}

func TestNPMPackumentIsRewrittenAndTarballsAreVerified(t *testing.T) {
	f := newFixture(t, nil)
	prefix := "/t/" + token(t, "t-a", 0)
	status, body, _ := f.get(prefix+"/npm/demo-pkg", nil)
	if status != 200 {
		t.Fatalf("%d %s", status, body)
	}
	var doc map[string]any
	_ = json.Unmarshal(body, &doc)
	v := doc["versions"].(map[string]any)["1.0.0"].(map[string]any)
	tarball := v["dist"].(map[string]any)["tarball"].(string)
	if tarball != f.http.URL+prefix+"/npm/demo-pkg/-/demo-pkg-1.0.0.tgz" {
		t.Fatalf("tarball 地址：%s", tarball)
	}
	for i := 0; i < 2; i++ {
		status, body, _ := f.get(strings.TrimPrefix(tarball, f.http.URL), nil)
		if status != 200 || !bytes.Equal(body, f.up.tgz) {
			t.Fatalf("tarball：%d", status)
		}
	}
	if f.up.hit("/npm/demo-pkg/-/demo-pkg-1.0.0.tgz") != 1 {
		t.Fatal("第二次从库里给")
	}
}

func TestGoProxyProtocolAndSumDBAreProxied(t *testing.T) {
	f := newFixture(t, nil)
	prefix := "/t/" + token(t, "t-a", 0)
	for _, p := range []string{"/go/example.com/demo/@v/list", "/go/example.com/demo/@v/v1.0.0.info", "/go/example.com/demo/@v/v1.0.0.mod",
		"/go/example.com/demo/@v/v1.0.0.zip", "/go/example.com/demo/@latest", "/go/sumdb/sum.golang.org/supported",
		"/go/sumdb/sum.golang.org/lookup/example.com/demo@v1.0.0"} {
		if status, body, _ := f.get(prefix+p, nil); status != 200 {
			t.Fatalf("%s：%d %s", p, status, body)
		}
	}
	f.get(prefix+"/go/example.com/demo/@v/v1.0.0.zip", nil)
	if f.up.hit("/go/example.com/demo/@v/v1.0.0.zip") != 1 {
		t.Fatal("模块 zip 只拉一次")
	}
	if status, _, _ := f.get(prefix+"/go/example.com/missing/@v/v1.0.0.zip", nil); status != http.StatusNotFound {
		t.Fatalf("上游没有的照样报 404（go 据此试下一个代理）：%d", status)
	}
}

func TestCredentialIsRequiredAndCanComeFromHeaders(t *testing.T) {
	f := newFixture(t, nil)
	if status, _, _ := f.get("/pypi/simple/demo-pkg/", nil); status != http.StatusUnauthorized {
		t.Fatalf("没有凭据：%d", status)
	}
	egress, _ := envcred.Issue(credKey, envcred.Claims{EnvRef: "sbe-0123456789abcdef", TenantID: "t-a", ExpiresAt: time.Now().Add(time.Hour).Unix(), Purpose: envcred.PurposeEgress})
	if status, _, _ := f.get("/t/"+egress+"/pypi/simple/demo-pkg/", nil); status != http.StatusUnauthorized {
		t.Fatal("出网网关的凭据不能用来访问包镜像")
	}
	tok := token(t, "t-a", 0)
	if status, _, _ := f.get("/pypi/simple/demo-pkg/", map[string]string{"Authorization": "Bearer " + tok}); status != 200 {
		t.Fatal("Bearer")
	}
	req, _ := http.NewRequest(http.MethodGet, f.http.URL+"/npm/demo-pkg", nil)
	req.SetBasicAuth("mirror", tok)
	if res, err := http.DefaultClient.Do(req); err != nil || res.StatusCode != 200 {
		t.Fatal("Basic")
	}
	if !strings.Contains(mirror.SanitizePath("/t/"+tok+"/pypi/simple/x/"), "/t/***/pypi") || strings.Contains(mirror.SanitizePath("/t/"+tok+"/x"), tok) {
		t.Fatal("日志里的路径不带凭据")
	}
}

func TestMissQuotaPassesThroughThenDeniesAndOtherTenantsAreUnaffected(t *testing.T) {
	f := newFixture(t, nil)
	small := token(t, "t-small", 1) // 1 字节：第一次未命中（索引）之后就超了两倍
	f.pypiJSON("/t/" + small)
	files := []string{"/files/packages/aa/bb/demo_pkg-1.0.0-py3-none-any.whl", "/files/packages/cc/dd/demo-pkg-0.9.tar.gz"}
	if used := f.usage.Used(context.Background(), "t-small", time.Now()); used <= 2 {
		t.Fatalf("索引的未命中要计入：%d", used)
	}
	big := token(t, "t-big", 0)
	path := "/t/" + big + "/pypi/files/" + strings.TrimPrefix(files[0], "/files/")
	if status, _, _ := f.get(path, nil); status != 200 {
		t.Fatal("别的租户不受影响")
	}
	if status, _, _ := f.get("/t/"+small+"/pypi/files/"+strings.TrimPrefix(files[1], "/files/"), nil); status != http.StatusTooManyRequests {
		t.Fatalf("到两倍时拒绝：%d", status)
	}
	// 命中不限：t-small 也能拿到已经在库里的文件。
	if status, body, _ := f.get("/t/"+small+"/pypi/files/"+strings.TrimPrefix(files[0], "/files/"), nil); status != 200 || !bytes.Equal(body, f.up.wheel) {
		t.Fatalf("命中不受未命中配额限制：%d", status)
	}
}

func TestQuotaBetweenOneAndTwoTimesOnlyPassesThrough(t *testing.T) {
	f := newFixture(t, nil)
	tok := token(t, "t-mid", 10_000) // 索引约几百字节，wheel 约 1 KB：第一次拉 wheel 前还在配额里
	f.usage.Add("t-mid", 12_000, time.Now())
	f.pypiJSON("/t/" + tok) // 元数据：在一倍到两倍之间时照样拉
	status, body, _ := f.get("/t/"+tok+"/pypi/files/packages/aa/bb/demo_pkg-1.0.0-py3-none-any.whl", nil)
	if status != 200 || !bytes.Equal(body, f.up.wheel) {
		t.Fatalf("超了一倍：照样给，%d", status)
	}
	if f.blobs.Stats().Files != 0 || f.srv.Metrics.Get("passthrough", "pypi") != 1 {
		t.Fatal("超了一倍：只转发、不进库")
	}
}

func TestOversizedFilesAreServedButNotCached(t *testing.T) {
	f := newFixture(t, func(o *cache.Options) { o.MaxFile = 100 })
	tok := token(t, "t-a", 0)
	status, body, _ := f.get("/t/"+tok+"/npm/demo-pkg/-/demo-pkg-1.0.0.tgz", nil)
	if status != 200 || !bytes.Equal(body, f.up.tgz) {
		t.Fatalf("超过大小上限的照样能装：%d", status)
	}
	if f.blobs.Stats().Files != 0 {
		t.Fatal("超过大小上限的不落盘")
	}
}

func TestPurgedVersionDisappearsForEveryone(t *testing.T) {
	f := newFixture(t, nil)
	tok := token(t, "t-a", 0)
	f.get("/t/"+tok+"/pypi/files/packages/aa/bb/demo_pkg-1.0.0-py3-none-any.whl", nil)
	f.get("/t/"+tok+"/npm/demo-pkg/-/demo-pkg-1.0.0.tgz", nil)
	post := func(body string, auth string) int {
		req, _ := http.NewRequest(http.MethodPost, f.admin.URL+"/admin/purge", strings.NewReader(body))
		if auth != "" {
			req.Header.Set("Authorization", "Bearer "+auth)
		}
		res, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		res.Body.Close()
		return res.StatusCode
	}
	if post(`{"ecosystem":"pypi","name":"demo-pkg","version":"1.0.0"}`, "") != http.StatusUnauthorized {
		t.Fatal("管理接口要令牌")
	}
	if post(`{"ecosystem":"pypi","name":"Demo_Pkg","version":"1.0.0","reason":"恶意"}`, adminToken) != 200 ||
		post(`{"ecosystem":"npm","name":"demo-pkg","version":"1.0.0"}`, adminToken) != 200 {
		t.Fatal("清除")
	}
	other := token(t, "t-other", 0)
	idx := f.pypiJSON("/t/" + other)
	for _, file := range idx["files"].([]any) {
		if strings.Contains(file.(map[string]any)["filename"].(string), "1.0.0") {
			t.Fatal("索引里不再列被拉黑的版本")
		}
	}
	if status, _, _ := f.get("/t/"+other+"/pypi/files/packages/aa/bb/demo_pkg-1.0.0-py3-none-any.whl", nil); status != http.StatusNotFound {
		t.Fatalf("被拉黑的文件 404：%d", status)
	}
	_, body, _ := f.get("/t/"+other+"/npm/demo-pkg", nil)
	var doc map[string]any
	_ = json.Unmarshal(body, &doc)
	if _, ok := doc["versions"].(map[string]any)["1.0.0"]; ok || doc["dist-tags"].(map[string]any)["latest"] != "0.9.0" {
		t.Fatalf("npm 的包信息去掉了被拉黑的版本，latest 换成剩下最高的：%v", doc["dist-tags"])
	}
	if f.blobs.Stats().Files != 0 {
		t.Fatalf("库里的文件删掉了：%d", f.blobs.Stats().Files)
	}
}

func TestConcurrentRequestsForOneFileFetchOnce(t *testing.T) {
	f := newFixture(t, nil)
	tok := token(t, "t-a", 0)
	var wg sync.WaitGroup
	errs := make(chan error, 10)
	for i := 0; i < 10; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			res, err := http.Get(f.http.URL + "/t/" + tok + "/go/example.com/demo/@v/v1.0.0.zip")
			if err != nil {
				errs <- err
				return
			}
			body, _ := io.ReadAll(res.Body)
			res.Body.Close()
			if res.StatusCode != 200 || !bytes.Equal(body, f.up.modZip) {
				errs <- fmt.Errorf("%d", res.StatusCode)
			}
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatal(err)
	}
	if n := f.up.hit("/go/example.com/demo/@v/v1.0.0.zip"); n != 1 {
		t.Fatalf("并发的请求只向上游拉一次，实际 %d 次", n)
	}
}

func TestStaleMetadataIsServedWhenTheUpstreamIsDown(t *testing.T) {
	f := newFixture(t, nil)
	tok := token(t, "t-a", 0)
	f.pypiJSON("/t/" + tok)
	f.up.mu.Lock()
	f.up.down = true
	f.up.mu.Unlock()
	// 元数据还新鲜：照常给；就算过期，上游不可用时也给旧的（缓存里 TTL 一分钟，这里只验证上游挂了照样能取索引）。
	if idx := f.pypiJSON("/t/" + tok); len(idx["files"].([]any)) != 2 {
		t.Fatal("上游不可用时照样给索引")
	}
}
