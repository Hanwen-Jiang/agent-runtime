package taskapi

// F3 验收（docs/plan/n18-file-delivery-f3.md 第 5 节第 2、3 条）：ListTaskArtifacts 与下载路由。
//
// task_artifacts 由迁移 207 建（Worker 的 agentruntime_worker.tools.deliver 与这里的 ArtifactMigrations 一致，契约测试比对），task-service 只读。
// 覆盖：只给元数据、不带对象地址；按创建时间排序；过期的标 EXPIRED；别的租户按不存在处理；下载每次重新核对租户、Task 与有效期；
// 响应头（attachment、媒体类型的允许列表、nosniff、sandbox、no-store）；内容与 sha256 不符时不给；没配 ObjectStore 时不可用。
// 实现之前 ListTaskArtifacts 返回未实现，这些测试跳过。

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"connectrpc.com/connect"
	"google.golang.org/protobuf/encoding/protojson"

	"github.com/agentruntime/agent-runtime/libs/go-core/dev/diskobjects"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/objectstore"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	taskapiv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/taskapi/v1"
)

type artifactFixture struct {
	*apiFixture
	objects *diskobjects.Provider
}

func newArtifactAPI(t *testing.T, withObjects bool) *artifactFixture {
	t.Helper()
	objects, err := diskobjects.New(diskobjects.Options{Root: t.TempDir(), SigningKey: []byte("object-signing-key-0123456789abc")})
	if err != nil {
		t.Fatal(err)
	}
	f := &artifactFixture{objects: objects}
	f.apiFixture = newAPIWith(t, func(d *Deps) {
		if withObjects {
			d.Objects = objects
		}
	})
	if err := f.store.Migrate(context.Background(), ArtifactMigrations()); err != nil {
		t.Fatal(err)
	}
	// 实现之前跳过。
	_, err = f.client.ListTaskArtifacts(context.Background(), authed(f.token("t1", "u1"), &taskapiv1.ListTaskArtifactsRequest{TaskId: "tsk_01J0000000000000000000000Z"}))
	if connect.CodeOf(err) == connect.CodeUnimplemented {
		t.Skip("ListTaskArtifacts 还没实现：先审验收测试（docs/plan/n18-file-delivery-f3.md）")
	}
	return f
}

type seeded struct {
	id, name, path, mediaType, sha, uri string
	data                                []byte
	created, expires                    time.Time
}

// seed 在租户 tenant 下给 Task taskID 放一个文件：对象写进 ObjectStore，元数据写进 task_artifacts。
func (f *artifactFixture) seed(tenant, taskID, artifactID, name, mediaType string, data []byte, created, expires time.Time) seeded {
	f.t.Helper()
	ctx := context.Background()
	scope := &commonv1.TenantContext{TenantId: tenant}
	objs, err := f.objects.Bind(scope)
	if err != nil {
		f.t.Fatal(err)
	}
	info, err := objs.Put(ctx, "deliveries/"+taskID+"/r4nd0m/"+name, data, objectstore.PutOptions{ContentType: mediaType})
	if err != nil {
		f.t.Fatal(err)
	}
	sum := sha256.Sum256(data)
	s := seeded{id: artifactID, name: name, path: "out/" + name, mediaType: mediaType, sha: hex.EncodeToString(sum[:]), uri: info.URI, data: data, created: created, expires: expires}
	store, err := f.store.Bind(scope)
	if err != nil {
		f.t.Fatal(err)
	}
	err = store.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		_, err := tx.Execute(ctx, `INSERT INTO task_artifacts (artifact_id, task_id, idempotency_key, name, path, media_type, size_bytes, sha256, object_uri, created_at, expires_at)
			VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)`,
			s.id, taskID, "idem-"+s.id, s.name, s.path, s.mediaType, int64(len(data)), s.sha, s.uri,
			created.UTC().Format("2006-01-02T15:04:05.000000Z"), expires.UTC().Format("2006-01-02T15:04:05.000000Z"))
		return err
	})
	if err != nil {
		f.t.Fatal(err)
	}
	return s
}

func (f *artifactFixture) list(token, tenantID, taskID string) (*taskapiv1.ListTaskArtifactsResponse, error) {
	resp, err := f.client.ListTaskArtifacts(context.Background(), authed(token, &taskapiv1.ListTaskArtifactsRequest{TenantId: tenantID, TaskId: taskID}))
	if err != nil {
		return nil, err
	}
	return resp.Msg, nil
}

func (f *artifactFixture) download(token, path string) (*http.Response, []byte) {
	f.t.Helper()
	req, err := http.NewRequest(http.MethodGet, f.url+path, nil)
	if err != nil {
		f.t.Fatal(err)
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		f.t.Fatal(err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		f.t.Fatal(err)
	}
	return resp, body
}

func artifactPath(taskID, artifactID string) string {
	return "/v1/tasks/" + taskID + "/artifacts/" + artifactID
}

const (
	artA = "art_01J00000000000000000000000"
	artB = "art_01J00000000000000000000001"
	artC = "art_01J00000000000000000000002"
)

func TestListTaskArtifactsGivesMetadataInCreationOrder(t *testing.T) {
	f := newArtifactAPI(t, true)
	token := f.token("t1", "u1")
	task := f.mustSubmit(token, newRequest("crid-artifacts")).TaskId
	other := f.mustSubmit(token, newRequest("crid-other")).TaskId
	now := f.clock.Now()
	later := f.seed("t1", task, artB, "chart.png", "image/png", []byte("png-bytes"), now.Add(time.Minute), now.Add(7*24*time.Hour))
	first := f.seed("t1", task, artA, "report.txt", "text/plain", []byte("hello report"), now, now.Add(7*24*time.Hour))
	f.seed("t1", other, artC, "other.txt", "text/plain", []byte("other task"), now, now.Add(7*24*time.Hour))

	got, err := f.list(token, "", task)
	if err != nil {
		t.Fatal(err)
	}
	if len(got.GetArtifacts()) != 2 {
		t.Fatalf("应只列这个 Task 的两个文件，得到 %v", got.GetArtifacts())
	}
	for i, want := range []seeded{first, later} {
		a := got.GetArtifacts()[i]
		if a.GetArtifactId() != want.id || a.GetName() != want.name || a.GetPath() != want.path || a.GetMediaType() != want.mediaType ||
			a.GetSizeBytes() != int64(len(want.data)) || a.GetSha256() != want.sha {
			t.Fatalf("第 %d 个元数据不对：%v", i, a)
		}
		if !a.GetCreatedAt().AsTime().Equal(want.created.Truncate(time.Microsecond)) || !a.GetExpiresAt().AsTime().Equal(want.expires.Truncate(time.Microsecond)) {
			t.Fatalf("第 %d 个的时间不对：%v", i, a)
		}
		if a.GetState() != taskapiv1.TaskArtifactState_TASK_ARTIFACT_STATE_READY {
			t.Fatalf("没过期的应是 READY：%v", a.GetState())
		}
		if a.GetDownloadPath() != artifactPath(task, want.id) {
			t.Fatalf("download_path 不对：%s", a.GetDownloadPath())
		}
	}
	raw, _ := protojson.Marshal(got)
	if strings.Contains(string(raw), first.uri) || strings.Contains(string(raw), "deliveries/") {
		t.Fatalf("响应里不能有对象存储的地址：%s", raw)
	}
}

func TestExpiredArtifactsAreListedAsExpired(t *testing.T) {
	f := newArtifactAPI(t, true)
	token := f.token("t1", "u1")
	task := f.mustSubmit(token, newRequest("crid-expired")).TaskId
	now := f.clock.Now()
	f.seed("t1", task, artA, "old.txt", "text/plain", []byte("old"), now.Add(-8*24*time.Hour), now.Add(-time.Hour))
	got, err := f.list(token, "", task)
	if err != nil {
		t.Fatal(err)
	}
	if len(got.GetArtifacts()) != 1 || got.GetArtifacts()[0].GetState() != taskapiv1.TaskArtifactState_TASK_ARTIFACT_STATE_EXPIRED {
		t.Fatalf("过期的应列出且标 EXPIRED：%v", got.GetArtifacts())
	}
}

func TestOtherTenantsCannotListArtifacts(t *testing.T) {
	f := newArtifactAPI(t, true)
	alice := f.token("t1", "u1")
	task := f.mustSubmit(alice, newRequest("crid-tenant")).TaskId
	now := f.clock.Now()
	f.seed("t1", task, artA, "report.txt", "text/plain", []byte("alice"), now, now.Add(time.Hour))
	// 别的租户下伪造同一个 task_id 的行：t1 看不到它。
	f.seed("t2", task, artB, "forged.txt", "text/plain", []byte("bob"), now, now.Add(time.Hour))

	bob := f.token("t2", "u2")
	_, err := f.list(bob, "", task)
	wantCode(t, "别的租户列这个 Task", err, connect.CodeNotFound)
	_, err = f.list(bob, "t1", task)
	wantCode(t, "请求里的租户与认证身份不一致", err, connect.CodePermissionDenied)
	_, err = f.list("", "", task)
	wantCode(t, "没有令牌", err, connect.CodeUnauthenticated)

	got, err := f.list(alice, "t1", task)
	if err != nil {
		t.Fatal(err)
	}
	if len(got.GetArtifacts()) != 1 || got.GetArtifacts()[0].GetArtifactId() != artA {
		t.Fatalf("t1 只能看到自己的那一个：%v", got.GetArtifacts())
	}
}

func TestDownloadGivesTheFileWithSafeHeaders(t *testing.T) {
	f := newArtifactAPI(t, true)
	token := f.token("t1", "u1")
	task := f.mustSubmit(token, newRequest("crid-download")).TaskId
	now := f.clock.Now()
	csv := f.seed("t1", task, artA, "季度报告.csv", "text/csv", []byte("q,rate\n3,0.12\n"), now, now.Add(time.Hour))
	page := f.seed("t1", task, artB, "page.html", "text/html", []byte("<script>alert(1)</script>"), now, now.Add(time.Hour))

	resp, body := f.download(token, artifactPath(task, csv.id))
	if resp.StatusCode != http.StatusOK || string(body) != string(csv.data) {
		t.Fatalf("应给出文件内容：%d %q", resp.StatusCode, body)
	}
	want := map[string]string{
		"Content-Type":            "text/csv",
		"Content-Disposition":     "attachment; filename*=UTF-8''%E5%AD%A3%E5%BA%A6%E6%8A%A5%E5%91%8A.csv",
		"X-Content-Type-Options":  "nosniff",
		"Content-Security-Policy": "sandbox",
		"Cache-Control":           "no-store",
	}
	for k, v := range want {
		if got := resp.Header.Get(k); got != v {
			t.Fatalf("%s 应是 %q，得到 %q", k, v, got)
		}
	}
	resp, body = f.download(token, artifactPath(task, page.id))
	if resp.StatusCode != http.StatusOK || string(body) != string(page.data) {
		t.Fatalf("应给出文件内容：%d", resp.StatusCode)
	}
	if got := resp.Header.Get("Content-Type"); got != "application/octet-stream" {
		t.Fatalf("不在允许列表里的媒体类型（text/html）应按 application/octet-stream 给，得到 %q", got)
	}
}

func TestDownloadIsRefusedOutsideItsScope(t *testing.T) {
	f := newArtifactAPI(t, true)
	alice := f.token("t1", "u1")
	task := f.mustSubmit(alice, newRequest("crid-scope")).TaskId
	other := f.mustSubmit(alice, newRequest("crid-scope-other")).TaskId
	now := f.clock.Now()
	mine := f.seed("t1", task, artA, "report.txt", "text/plain", []byte("secret report"), now, now.Add(time.Hour))
	theirs := f.seed("t1", other, artB, "other.txt", "text/plain", []byte("other task"), now, now.Add(time.Hour))
	old := f.seed("t1", task, artC, "old.txt", "text/plain", []byte("old"), now.Add(-2*time.Hour), now.Add(-time.Hour))

	cases := []struct {
		what, token, path string
		status            int
	}{
		{"没有令牌", "", artifactPath(task, mine.id), http.StatusUnauthorized},
		{"别的租户", f.token("t2", "u2"), artifactPath(task, mine.id), http.StatusNotFound},
		{"别的 Task 的文件挂在这个 Task 下", alice, artifactPath(task, theirs.id), http.StatusNotFound},
		{"不存在的文件", alice, artifactPath(task, "art_01J00000000000000000000009"), http.StatusNotFound},
		{"不合格式的编号", alice, artifactPath(task, "..%2F..%2Fsecrets"), http.StatusNotFound},
		{"过期", alice, artifactPath(task, old.id), http.StatusGone},
	}
	for _, c := range cases {
		resp, body := f.download(c.token, c.path)
		if resp.StatusCode != c.status {
			t.Fatalf("%s：应返回 %d，得到 %d（%s）", c.what, c.status, resp.StatusCode, body)
		}
		for _, leak := range []string{mine.uri, theirs.uri, old.uri, "deliveries/", "secret report"} {
			if strings.Contains(string(body), leak) {
				t.Fatalf("%s：错误里不能带 %q：%s", c.what, leak, body)
			}
		}
	}
}

func TestDownloadRefusesContentThatNoLongerMatches(t *testing.T) {
	f := newArtifactAPI(t, true)
	token := f.token("t1", "u1")
	task := f.mustSubmit(token, newRequest("crid-tamper")).TaskId
	now := f.clock.Now()
	s := f.seed("t1", task, artA, "report.txt", "text/plain", []byte("original"), now, now.Add(time.Hour))
	objs, err := f.objects.Bind(&commonv1.TenantContext{TenantId: "t1"})
	if err != nil {
		t.Fatal(err)
	}
	key, err := objs.KeyOf(s.uri)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := objs.Put(context.Background(), key, []byte("tampered"), objectstore.PutOptions{ContentType: "text/plain"}); err != nil {
		t.Fatal(err)
	}
	resp, body := f.download(token, artifactPath(task, s.id))
	if resp.StatusCode < 500 || strings.Contains(string(body), "tampered") {
		t.Fatalf("内容与 sha256 不符时不能给出：%d %q", resp.StatusCode, body)
	}
}

func TestDownloadWithoutObjectStoreIsUnavailable(t *testing.T) {
	f := newArtifactAPI(t, false)
	token := f.token("t1", "u1")
	task := f.mustSubmit(token, newRequest("crid-noobjects")).TaskId
	now := f.clock.Now()
	s := f.seed("t1", task, artA, "report.txt", "text/plain", []byte("report"), now, now.Add(time.Hour))
	if resp, _ := f.download(token, artifactPath(task, s.id)); resp.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("没配 ObjectStore 时应返回 503，得到 %d", resp.StatusCode)
	}
}
