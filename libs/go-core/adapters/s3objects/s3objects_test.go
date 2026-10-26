package s3objects_test

// D-09：ObjectStore 的 S3 兼容实现（docs/plan/cloud-deploy.md），与 Python 侧 tests/adapters/test_s3_object_store.py 对应：
//   - 一致性测试对着假服务跑（它按收到的原始请求重算签名、核对预签名地址的过期）；带前缀再跑一遍。
//   - SigV4 用 AWS 文档里的例子核对。
//   - S3 特有的：布局与 Python 侧相同；前缀隔开环境；生命周期按整天、只改本作用域的规则；密钥不出现在错误里。
//   - 设 AR_LIVE_S3=1（另要 AR_S3_ENDPOINT、AR_S3_BUCKET、AR_S3_ACCESS_KEY_ID、AR_S3_SECRET_ACCESS_KEY，可选 AR_S3_REGION）
//     时对真实的 R2/S3/MinIO 再跑一遍一致性测试。

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/s3objects"
	"github.com/agentruntime/agent-runtime/libs/go-core/conformance"
	"github.com/agentruntime/agent-runtime/libs/go-core/conformance/objectstoretest"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/objectstore"
	"github.com/agentruntime/agent-runtime/libs/go-core/secret"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

const (
	bucket    = "ar-objects"
	access    = "AKIDCONFORMANCE"
	secretKey = "conformance-secret-key-do-not-log"
)

// ruleIDForTA 是作用域 t-a（无前缀）的生命周期规则 ID 前缀；Python 侧的测试核对同一个值。
const ruleIDForTA = "ar-a90eb0c6bad3bc9105a538b4-"

var ctx = context.Background()

func sha256Hex(s string) string {
	sum := sha256.Sum256([]byte(s))
	return hex.EncodeToString(sum[:])
}

func fetch(rawURL string, data []byte) ([]byte, error) {
	method := http.MethodGet
	if data != nil {
		method = http.MethodPut
	}
	req, err := http.NewRequestWithContext(ctx, method, rawURL, bytes.NewReader(data))
	if err != nil {
		return nil, err
	}
	resp, err := (&http.Client{Transport: &http.Transport{Proxy: nil}}).Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	switch {
	case resp.StatusCode == http.StatusForbidden || resp.StatusCode == http.StatusUnauthorized:
		return nil, providererr.New(providererr.Denied, "expired_url", "临时地址无效或已过期")
	case resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusNoContent:
		return nil, providererr.New(providererr.Invalid, "object_store_invalid", fmt.Sprint("HTTP ", resp.StatusCode))
	}
	return body, nil
}

type harness struct {
	provider *s3objects.Provider
	fake     *s3objects.FakeS3
	clock    *testkit.FakeClock
	faults   *testkit.Faults
	advance  func(time.Duration)
}

func (h *harness) Provider() objectstore.Provider             { return h.provider }
func (h *harness) Advance(_ context.Context, d time.Duration) { h.advance(d) }
func (h *harness) InjectFault(op string, c providererr.Category) error {
	h.faults.FailNext(op, c)
	return nil
}
func (h *harness) Fetch(_ context.Context, u string) ([]byte, error) { return fetch(u, nil) }
func (h *harness) Upload(_ context.Context, u string, data []byte) error {
	if data == nil {
		data = []byte{}
	}
	_, err := fetch(u, data)
	return err
}

// RunLifecycle：S3 的生命周期按整天、由存储服务每天跑，不能立即触发；按天清理由 TestLifecycleExpiresByWholeDays 用假服务的时钟测。
func (h *harness) RunLifecycle(context.Context) error { return conformance.ErrUnsupported }

func newHarness(t *testing.T, prefix string) *harness {
	h := &harness{clock: testkit.NewFakeClock(), faults: testkit.NewFaults()}
	h.advance = h.clock.Advance
	h.fake = s3objects.StartFakeS3(t, bucket, access, secretKey, h.clock.Now)
	h.provider = newProvider(t, h, prefix, secretKey)
	return h
}

func newProvider(t *testing.T, h *harness, prefix, key string) *s3objects.Provider {
	p := conformance.Must(s3objects.New(s3objects.Options{
		Endpoint: h.fake.URL, Bucket: bucket, AccessKeyID: access, SecretAccessKey: secret.New(key),
		Prefix: prefix, Clock: h.clock, Faults: h.faults,
	}))
	t.Cleanup(func() { _ = p.Close() })
	return p
}

func TestConformance(t *testing.T) {
	objectstoretest.Run(t, func(t *testing.T) objectstoretest.Harness { return newHarness(t, "") })
}

func TestConformanceWithPrefix(t *testing.T) {
	objectstoretest.Run(t, func(t *testing.T) objectstoretest.Harness { return newHarness(t, "staging/") })
}

// --- SigV4：AWS 文档里的例子 ---

const (
	docSecretIAM = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
	docSecretS3  = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
)

var s3DocTime = time.Date(2013, 5, 24, 0, 0, 0, 0, time.UTC)

func TestIAMGetMatchesTheAWSExample(t *testing.T) {
	req, _ := http.NewRequest(http.MethodGet, "https://iam.amazonaws.com/?Action=ListUsers&Version=2010-05-08", nil)
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded; charset=utf-8")
	s3objects.Sign(req, s3objects.EmptySHA256, "AKIDEXAMPLE", secret.New(docSecretIAM), "us-east-1", "iam", time.Date(2015, 8, 30, 12, 36, 0, 0, time.UTC))
	want := "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/iam/aws4_request, SignedHeaders=content-type;host;x-amz-date, " +
		"Signature=5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7"
	if got := req.Header.Get("Authorization"); got != want {
		t.Fatalf("Authorization = %q", got)
	}
}

func TestS3RequestsMatchTheAWSExamples(t *testing.T) {
	cases := []struct {
		name, method, url string
		header            map[string]string
		body              string
		want              string
	}{
		{"get_object", "GET", "https://examplebucket.s3.amazonaws.com/test.txt", map[string]string{"Range": "bytes=0-9"}, "",
			"f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"},
		{"put_object", "PUT", "https://examplebucket.s3.amazonaws.com/" + s3objects.Escape("test$file.text", true),
			map[string]string{"Date": "Fri, 24 May 2013 00:00:00 GMT", "X-Amz-Storage-Class": "REDUCED_REDUNDANCY"}, "Welcome to Amazon S3.",
			"98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd"},
		{"get_lifecycle", "GET", "https://examplebucket.s3.amazonaws.com/?lifecycle", nil, "",
			"fea454ca298b7da1c68078a5d1bdbfbbe0d65c699e0f91ac7a200a0136783543"},
		{"list_objects", "GET", "https://examplebucket.s3.amazonaws.com/?max-keys=2&prefix=J", nil, "",
			"34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			req, _ := http.NewRequest(c.method, c.url, strings.NewReader(c.body))
			for k, v := range c.header {
				req.Header.Set(k, v)
			}
			s3objects.Sign(req, sha256Hex(c.body), "AKIAIOSFODNN7EXAMPLE", secret.New(docSecretS3), "us-east-1", "s3", s3DocTime)
			if got := req.Header.Get("Authorization"); !strings.HasSuffix(got, "Signature="+c.want) {
				t.Fatalf("Authorization = %q", got)
			}
		})
	}
}

func TestPresignedURLMatchesTheAWSExample(t *testing.T) {
	u := conformance.Must(s3objects.Presign("GET", "https://examplebucket.s3.amazonaws.com/test.txt", 86400, "AKIAIOSFODNN7EXAMPLE",
		secret.New(docSecretS3), "us-east-1", "s3", s3DocTime))
	if !strings.HasSuffix(u, "X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404") ||
		!strings.Contains(u, "X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20130524%2Fus-east-1%2Fs3%2Faws4_request") {
		t.Fatalf("url = %s", u)
	}
}

// --- S3 特有的规则 ---

func tenant(id string) *commonv1.TenantContext { return &commonv1.TenantContext{TenantId: id} }

func TestWrongSecretIsDeniedAndNeverEchoed(t *testing.T) {
	h := newHarness(t, "")
	bad := newProvider(t, h, "", "a-different-secret-value")
	_, err := conformance.Must(bad.Bind(conformance.TenantA())).Put(ctx, "k", []byte("x"), objectstore.PutOptions{})
	conformance.ExpectError(t, err, providererr.Denied, "")
	if msg := fmt.Sprintf("%v %+v %#v", err, bad, bad); strings.Contains(msg, "a-different-secret-value") || strings.Contains(msg, secretKey) {
		t.Fatalf("secret leaked: %s", msg)
	}
}

func TestKeysAreLaidOutLikeThePythonAdapter(t *testing.T) {
	h := newHarness(t, "")
	conformance.Must(conformance.Must(h.provider.Bind(conformance.TenantA())).Put(ctx, "reports/华东 q3.csv", []byte("x"), objectstore.PutOptions{}))
	staging := newProvider(t, h, "staging/", secretKey)
	conformance.Must(conformance.Must(staging.BindPlatform("evals")).Put(ctx, "set.json", []byte("y"), objectstore.PutOptions{}))
	if got := strings.Join(h.fake.Names(), ","); got != "staging/_platform/set.json,t-alpha/reports/华东 q3.csv" {
		t.Fatalf("names = %s", got)
	}
	// 生命周期规则 ID 两边一样（Python 侧 test_rule_ids_match_the_go_adapter）。
	if got := s3objects.RuleIDPrefix("t-a/"); got != ruleIDForTA {
		t.Fatalf("RuleIDPrefix = %s", got)
	}
}

func TestPrefixesKeepEnvironmentsApart(t *testing.T) {
	h := newHarness(t, "")
	prod := conformance.Must(newProvider(t, h, "prod/", secretKey).Bind(conformance.TenantA()))
	staging := conformance.Must(newProvider(t, h, "staging/", secretKey).Bind(conformance.TenantA()))
	conformance.Must(prod.Put(ctx, "k", []byte("prod"), objectstore.PutOptions{}))
	if items := conformance.Must(staging.List(ctx, objectstore.ListOptions{})).Items; len(items) != 0 {
		t.Fatalf("staging listed %d objects of prod", len(items))
	}
	_, err := staging.Get(ctx, "k")
	conformance.ExpectError(t, err, providererr.NotFound, "")
}

func TestOptionsAreChecked(t *testing.T) {
	base := s3objects.Options{Endpoint: "https://example.invalid", Bucket: "b", AccessKeyID: "a", SecretAccessKey: secret.New("s")}
	for _, mutate := range []func(*s3objects.Options){
		func(o *s3objects.Options) { o.Prefix = "staging" },
		func(o *s3objects.Options) { o.Prefix = "/staging/" },
		func(o *s3objects.Options) { o.Endpoint = "http://r2.example.com" },
		func(o *s3objects.Options) { o.SecretAccessKey = secret.Value{} },
	} {
		o := base
		mutate(&o)
		if _, err := s3objects.New(o); err == nil {
			t.Fatalf("New(%+v) should fail", o)
		}
	}
}

func TestOptionsFromEnv(t *testing.T) {
	env := func(m map[string]string) func(string) string { return func(k string) string { return m[k] } }
	if _, ok, err := s3objects.OptionsFromEnv(env(nil)); ok || err != nil {
		t.Fatal("没设 AR_S3_ENDPOINT 时不用 S3")
	}
	_, _, err := s3objects.OptionsFromEnv(env(map[string]string{s3objects.EnvEndpoint: "https://x.r2.cloudflarestorage.com", s3objects.EnvSecretAccessKey: "top-secret"}))
	if err == nil || !strings.Contains(err.Error(), s3objects.EnvBucket) || !strings.Contains(err.Error(), s3objects.EnvAccessKeyID) || strings.Contains(err.Error(), "top-secret") {
		t.Fatalf("err = %v", err)
	}
	file := t.TempDir() + "/key"
	conformance.NoError(t, os.WriteFile(file, []byte("from-file\n"), 0o600))
	opts, ok, err := s3objects.OptionsFromEnv(env(map[string]string{
		s3objects.EnvEndpoint: "https://x.r2.cloudflarestorage.com", s3objects.EnvBucket: "b", s3objects.EnvAccessKeyID: "a",
		s3objects.EnvSecretAccessKeyFile: file, s3objects.EnvPrefix: "prod/",
	}))
	if !ok || err != nil || opts.SecretAccessKey.RevealString() != "from-file" || opts.Prefix != "prod/" || opts.Bucket != "b" {
		t.Fatalf("opts = %+v, ok = %v, err = %v", opts, ok, err)
	}
	if strings.Contains(fmt.Sprintf("%+v", opts), "from-file") {
		t.Fatal("Options 打印出了密钥")
	}
}

func TestPresignedUploadReportsSHA256OnGet(t *testing.T) {
	h := newHarness(t, "")
	s := conformance.Must(h.provider.Bind(conformance.TenantA()))
	conformance.NoError(t, h.Upload(ctx, conformance.Must(s.TemporaryURL(ctx, "up", 5*time.Minute, "PUT")), []byte("from-browser")))
	if conformance.Must(s.Head(ctx, "up")).SHA256 != "" {
		t.Fatal("presigned upload should have no sha256 metadata")
	}
	if conformance.Must(s.Get(ctx, "up")).Info.SHA256 != sha256Hex("from-browser") {
		t.Fatal("Get should compute sha256 from the content")
	}
}

func TestPresignedURLIsScopedToItsKeyAndMethod(t *testing.T) {
	h := newHarness(t, "")
	s := conformance.Must(h.provider.Bind(conformance.TenantA()))
	conformance.Must(s.Put(ctx, "k", []byte("secret"), objectstore.PutOptions{}))
	u := conformance.Must(s.TemporaryURL(ctx, "k", 5*time.Minute, "GET"))
	conformance.ExpectError(t, h.Upload(ctx, u, []byte("overwrite")), providererr.Denied, "")
	_, err := h.Fetch(ctx, strings.Replace(u, "/t-alpha/k?", "/t-beta/k?", 1))
	conformance.ExpectError(t, err, providererr.Denied, "")
	long := conformance.Must(s.TemporaryURL(ctx, "k", 30*24*time.Hour, "GET"))
	if q := conformance.Must(url.Parse(long)).Query(); q.Get("X-Amz-Expires") != "604800" {
		t.Fatalf("X-Amz-Expires = %s", q.Get("X-Amz-Expires"))
	}
}

func TestListLimitAboveAPageIsCapped(t *testing.T) {
	h := newHarness(t, "")
	s := conformance.Must(h.provider.Bind(conformance.TenantA()))
	conformance.Must(s.Put(ctx, "a", []byte("x"), objectstore.PutOptions{}))
	if items := conformance.Must(s.List(ctx, objectstore.ListOptions{Limit: 5000})).Items; len(items) != 1 {
		t.Fatalf("items = %+v", items)
	}
}

func TestLifecycleExpiresByWholeDays(t *testing.T) {
	h := newHarness(t, "")
	s := conformance.Must(h.provider.Bind(conformance.TenantA()))
	conformance.NoError(t, s.SetLifecycle(ctx, []objectstore.LifecycleRule{{Prefix: "tmp/", ExpireAfter: time.Hour}}))
	conformance.Must(s.Put(ctx, "tmp/old", []byte("x"), objectstore.PutOptions{}))
	conformance.Must(s.Put(ctx, "keep/old", []byte("x"), objectstore.PutOptions{}))
	h.clock.Advance(2 * time.Hour)
	h.fake.RunLifecycle()
	conformance.Must(s.Get(ctx, "tmp/old")) // 不足一天按一天：两小时后还在
	h.clock.Advance(24 * time.Hour)
	h.fake.RunLifecycle()
	_, err := s.Get(ctx, "tmp/old")
	conformance.ExpectError(t, err, providererr.NotFound, "")
	conformance.Must(s.Get(ctx, "keep/old"))
}

func TestLifecycleOnlyReplacesRulesOfItsOwnScope(t *testing.T) {
	h := newHarness(t, "")
	short := conformance.Must(h.provider.Bind(tenant("t-a")))
	longer := conformance.Must(h.provider.Bind(tenant("t-a-b")))
	platform := conformance.Must(h.provider.BindPlatform("evals"))
	conformance.NoError(t, longer.SetLifecycle(ctx, []objectstore.LifecycleRule{{Prefix: "tmp/", ExpireAfter: 72 * time.Hour}}))
	conformance.NoError(t, platform.SetLifecycle(ctx, []objectstore.LifecycleRule{{Prefix: "", ExpireAfter: 30 * 24 * time.Hour}}))
	conformance.NoError(t, short.SetLifecycle(ctx, []objectstore.LifecycleRule{{Prefix: "tmp/", ExpireAfter: 24 * time.Hour}}))
	conformance.NoError(t, short.SetLifecycle(ctx, []objectstore.LifecycleRule{{Prefix: "cache/", ExpireAfter: 48 * time.Hour}}))
	cfg := string(h.fake.Lifecycle())
	if strings.Count(cfg, "<Rule>") != 3 || !strings.Contains(cfg, "<Prefix>t-a/cache/</Prefix>") || strings.Contains(cfg, "<Prefix>t-a/tmp/</Prefix>") ||
		!strings.Contains(cfg, "<Prefix>t-a-b/tmp/</Prefix>") || !strings.Contains(cfg, "<Prefix>_platform/</Prefix>") {
		t.Fatalf("lifecycle = %s", cfg)
	}
	conformance.NoError(t, short.SetLifecycle(ctx, nil))
	conformance.NoError(t, longer.SetLifecycle(ctx, nil))
	conformance.NoError(t, platform.SetLifecycle(ctx, nil))
	if h.fake.Lifecycle() != nil {
		t.Fatal("没有规则了就删掉整份配置（S3 不接受空的生命周期配置）")
	}
}

func TestUnreachableStoreIsTransient(t *testing.T) {
	p := conformance.Must(s3objects.New(s3objects.Options{Endpoint: "http://127.0.0.1:9", Bucket: "b", AccessKeyID: "a", SecretAccessKey: secret.New("s"),
		HTTPClient: &http.Client{Timeout: 2 * time.Second}}))
	_, err := conformance.Must(p.Bind(conformance.TenantA())).Get(ctx, "k")
	conformance.ExpectError(t, err, providererr.Transient, "object_store_unavailable")
}

// --- 真实的存储服务 ---

func TestLiveConformance(t *testing.T) {
	if os.Getenv("AR_LIVE_S3") != "1" {
		t.Skip("设 AR_LIVE_S3=1 与 AR_S3_* 才对真实存储跑")
	}
	objectstoretest.Run(t, func(t *testing.T) objectstoretest.Harness {
		raw := make([]byte, 16)
		_, _ = rand.Read(raw)
		h := &harness{faults: testkit.NewFaults()}
		h.advance = func(d time.Duration) {
			if d > 2*time.Minute {
				t.Skip("真实存储上不等这么久")
			}
			time.Sleep(d)
		}
		h.provider = conformance.Must(s3objects.New(s3objects.Options{
			Endpoint: os.Getenv("AR_S3_ENDPOINT"), Bucket: os.Getenv("AR_S3_BUCKET"), AccessKeyID: os.Getenv("AR_S3_ACCESS_KEY_ID"),
			SecretAccessKey: secret.New(os.Getenv("AR_S3_SECRET_ACCESS_KEY")), Region: os.Getenv("AR_S3_REGION"),
			Prefix: "conformance/" + hex.EncodeToString(raw) + "/", Faults: h.faults,
		}))
		t.Cleanup(func() {
			for _, bind := range []func() (objectstore.Store, error){
				func() (objectstore.Store, error) { return h.provider.Bind(conformance.TenantA()) },
				func() (objectstore.Store, error) { return h.provider.Bind(conformance.TenantB()) },
				func() (objectstore.Store, error) { return h.provider.BindPlatform("conformance") },
			} {
				s := conformance.Must(bind())
				if page, err := s.List(ctx, objectstore.ListOptions{}); err == nil {
					for _, item := range page.Items {
						_, _ = s.Delete(ctx, item.Key)
					}
				}
				_ = s.SetLifecycle(ctx, nil)
			}
		})
		return h
	})
}
