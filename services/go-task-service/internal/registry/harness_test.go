package registry_test

// T-08 验收的测试支撑（细化见 docs/plan/registry.md）。同一组用例分别跑在开发实现（SQLite，每个作用域一个库）
// 与生产实现（Postgres，行级安全）上；Registry 挂在明文 HTTP/2 的 httptest 服务上，用 connect 的 gRPC 客户端调用
// （与 Python 的 grpcio 同一种协议，trailer 也经线上传输）。发布令牌放在开发实现的 SecretStore 的平台作用域。

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"testing"

	"connectrpc.com/connect"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/structpb"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/postgres"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/postgres/pgtest"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/filesecrets"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/sqlitestore"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	registryv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/registry/v1"
	"github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/registry/v1/registryv1connect"
	workflowv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workflow/v1"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/registry"
)

func TestMain(m *testing.M) { os.Exit(pgtest.Main(m)) }

// publishToken 有辨识度：断言它不出现在错误与日志里。
const publishToken = "reg-publish-7c1e9a-should-never-be-printed"

// errorTrailer 照约定写死（与 Model Gateway 相同，Python 侧据此还原 ProviderError），不从被测包取，用来核对约定。
const errorTrailer = "ar-error-bin"

var (
	t1 = &commonv1.TenantContext{TenantId: "t1", UserId: "u1"}
	t2 = &commonv1.TenantContext{TenantId: "t2", UserId: "u2"}
	// schemaDir 是仓库根下的 /schemas：Registry 按它校验 document。
	schemaDir   = filepath.Join("..", "..", "..", "..", "schemas")
	examplesDir = filepath.Join(schemaDir, "examples")
)

const (
	kindTemplate  = registryv1.RegistryKind_REGISTRY_KIND_NODE_TEMPLATE
	kindRecipe    = registryv1.RegistryKind_REGISTRY_KIND_RECIPE
	kindReasoning = registryv1.RegistryKind_REGISTRY_KIND_REASONING_TEMPLATE
	kindProfile   = registryv1.RegistryKind_REGISTRY_KIND_MODEL_PROFILE
	kindPolicy    = registryv1.RegistryKind_REGISTRY_KIND_TENANT_POLICY

	stateShadow  = registryv1.ReleaseState_RELEASE_STATE_SHADOW
	stateDefault = registryv1.ReleaseState_RELEASE_STATE_DEFAULT
	stateRetired = registryv1.ReleaseState_RELEASE_STATE_RETIRED
)

// --- 文档 ---

const (
	answerDoc       = `{"description":"直接作答","executor":"model","effect":"none","role":"deliver","output":"generate"}`
	answerDocV2     = `{"description":"直接作答，先列要点","executor":"model","effect":"none","role":"deliver","output":"generate"}`
	judgeDoc        = `{"description":"按 rubric 打分","executor":"model","effect":"none","role":"judge","output":"score"}`
	sqlQueryDoc     = `{"description":"查询分析库","executor":"program","effect":"read","role":"fetch","params_schema":{"type":"object","required":["sql"],"properties":{"sql":{"type":"string"}}},"defaults":{"cache":{"ttl_s":600},"retry":{"max":2}}}`
	ledgerDeleteDoc = `{"description":"删除台账条目","executor":"program","effect":"write_irreversible","role":"act","defaults":{"retry":{"max":0}}}`
	ledgerWriteDoc  = `{"description":"写入台账","executor":"program","effect":"write_reversible","role":"act","rollback_template":"ledger_delete","supports_idempotency_lookup":true}`

	profileID  = "a6api/gpt-4o-mini@2026-10"
	profileDoc = `{"id":"a6api/gpt-4o-mini@2026-10","vendor":"openai","model":"gpt-4o-mini","class":"small",` +
		`"tiers":[{"name":"low","effort":"","thinking_budget":0}],"capabilities":["generate","structured","tools","stream"],` +
		`"cost":{"in_per_m":0.15,"out_per_m":0.6,"cache_read_per_m":0.075},"constraints":{"context_window":128000,"max_output":16384},` +
		`"freshness":{"price_at":"2026-10-21"}}`

	policyDoc   = `{"effect_ceiling":"write_reversible","model_allowlist":["a6api/gpt-4o-mini@2026-10"]}`
	policyDocV2 = `{"effect_ceiling":"read"}`

	// passingEvidence 是合 schema 且达标的评测证据。
	passingEvidence = `{"eval_set":{"name":"task-loop","version":"c0117be"},"samples":26,` +
		`"candidate":{"success_rate":0.92,"cost_per_success_usd":0.05},"passed":true,"report":"r.json","evaluated_at":"2026-10-22T09:00:00Z"}`
	failingEvidence = `{"eval_set":{"name":"task-loop","version":"c0117be"},"samples":26,` +
		`"baseline":{"success_rate":0.9,"cost_per_success_usd":0.05},"candidate":{"success_rate":0.6,"cost_per_success_usd":0.05},` +
		`"passed":false,"report":"r.json","evaluated_at":"2026-10-22T09:00:00Z"}`
)

// recipeDoc 是一个只有一个 Stage 的 Recipe：每个模板引用一个 atomic 节点 n1、n2……，依次相连。
func recipeDoc(t testing.TB, family, risk string, templates ...string) string {
	t.Helper()
	nodes := make([]map[string]any, 0, len(templates))
	for i, ref := range templates {
		node := map[string]any{"id": "n" + string(rune('1'+i)), "kind": "atomic", "template": ref}
		if i > 0 {
			node["after"] = []string{"n" + string(rune('0'+i))}
		}
		nodes = append(nodes, node)
	}
	return mustJSON(t, map[string]any{
		"task_family": family,
		"description": "测试用的做法",
		"risk":        risk,
		"stages":      []any{map[string]any{"goal": "完成", "exit_when": "完成", "nodes": nodes}},
	})
}

func mustJSON(t testing.TB, v any) string {
	t.Helper()
	data, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

// payload 把 JSON 文本包成 JsonPayload（schema_id 留空：Registry 按种类认定）。
func payload(t testing.TB, js string) *commonv1.JsonPayload {
	t.Helper()
	var v structpb.Value
	if err := protojson.Unmarshal([]byte(js), &v); err != nil {
		t.Fatalf("测试文档不是合法 JSON：%v\n%s", err, js)
	}
	return &commonv1.JsonPayload{Value: &v}
}

func value(t testing.TB, js string) *structpb.Value {
	t.Helper()
	return payload(t, js).GetValue()
}

// --- 后端与 Registry ---

type backend struct {
	name string
	open func(t *testing.T) relational.Provider
}

var backends = []backend{
	{"sqlite", func(t *testing.T) relational.Provider {
		p, err := sqlitestore.New(sqlitestore.Options{Dir: t.TempDir()})
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = p.Close() })
		return p
	}},
	{"postgres", func(t *testing.T) relational.Provider {
		c := pgtest.Current(t)
		p, err := postgres.New(context.Background(), postgres.Options{DSN: c.DSN(pgtest.AppRole, c.NewDatabase(t))})
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = p.Close() })
		return p
	}},
}

// syncBuffer 收集 Registry 的日志，供“令牌不进日志”的断言使用。
type syncBuffer struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (s *syncBuffer) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.Write(p)
}

func (s *syncBuffer) String() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.String()
}

type harness struct {
	t      *testing.T
	store  relational.Provider
	client registryv1connect.RegistryServiceClient
	logs   *syncBuffer
}

// newHarness 迁移业务库、构造 Registry（发布令牌写进 SecretStore 的平台作用域）并挂上 gRPC 服务。
// configure 可以改 Options（例如换掉 SecretStore）。
func newHarness(t *testing.T, store relational.Provider, configure ...func(*registry.Options)) *harness {
	t.Helper()
	secrets, err := filesecrets.New(filesecrets.Options{Path: filepath.Join(t.TempDir(), "secrets.json")})
	if err != nil {
		t.Fatal(err)
	}
	secrets.SeedSecret("", registry.PublishTokenSecret, publishToken)
	logs := &syncBuffer{}
	opts := registry.Options{
		Store:     store,
		Secrets:   secrets,
		SchemaDir: schemaDir,
		Logger:    slog.New(slog.NewTextHandler(logs, &slog.HandlerOptions{Level: slog.LevelDebug})),
	}
	for _, c := range configure {
		c(&opts)
	}
	if err := store.Migrate(context.Background(), registry.Migrations()); err != nil {
		t.Fatal(err)
	}
	reg, err := registry.New(opts)
	if err != nil {
		t.Fatal(err)
	}
	mux := http.NewServeMux()
	mux.Handle(registryv1connect.NewRegistryServiceHandler(reg))
	ts := httptest.NewUnstartedServer(mux)
	serverProtocols := new(http.Protocols)
	serverProtocols.SetHTTP1(true)
	serverProtocols.SetUnencryptedHTTP2(true)
	ts.Config.Protocols = serverProtocols
	ts.Start()
	t.Cleanup(ts.Close)
	clientProtocols := new(http.Protocols)
	clientProtocols.SetUnencryptedHTTP2(true)
	httpClient := &http.Client{Transport: &http.Transport{Protocols: clientProtocols}}
	t.Cleanup(httpClient.CloseIdleConnections)
	return &harness{
		t: t, store: store, logs: logs,
		client: registryv1connect.NewRegistryServiceClient(httpClient, ts.URL, connect.WithGRPC()),
	}
}

// eachBackend 在两种后端上各跑一遍；Postgres 缺二进制时按 AR_ALLOW_MISSING_TOOLS 跳过（pgtest）。
func eachBackend(t *testing.T, fn func(t *testing.T, h *harness)) {
	for _, b := range backends {
		t.Run(b.name, func(t *testing.T) { fn(t, newHarness(t, b.open(t))) })
	}
}

// --- 调用 ---

// withToken 带上发布令牌（gRPC 元数据 authorization: Bearer …）；token 为空时不带。
func withToken[T any](msg *T, token string) *connect.Request[T] {
	req := connect.NewRequest(msg)
	if token != "" {
		req.Header().Set("Authorization", "Bearer "+token)
	}
	return req
}

func (h *harness) publishAs(token string, tenant *commonv1.TenantContext, kind registryv1.RegistryKind, name, doc string) (*registryv1.PublishResponse, error) {
	resp, err := h.client.Publish(context.Background(), withToken(&registryv1.PublishRequest{
		Tenant: tenant, Kind: kind, Name: name, Document: payload(h.t, doc), ChangeNote: "测试",
	}, token))
	if err != nil {
		return nil, err
	}
	return resp.Msg, nil
}

func (h *harness) publish(tenant *commonv1.TenantContext, kind registryv1.RegistryKind, name, doc string) (*registryv1.PublishResponse, error) {
	return h.publishAs(publishToken, tenant, kind, name, doc)
}

// mustPublish 发布并返回版本号。
func (h *harness) mustPublish(tenant *commonv1.TenantContext, kind registryv1.RegistryKind, name, doc string) string {
	h.t.Helper()
	resp, err := h.publish(tenant, kind, name, doc)
	if err != nil {
		h.t.Fatalf("发布 %s %s 失败：%v", kind, name, err)
	}
	return resp.GetVersion()
}

func (h *harness) promoteAs(token string, tenant *commonv1.TenantContext, kind registryv1.RegistryKind, name, version, evidence string) (*registryv1.PromoteResponse, error) {
	req := &registryv1.PromoteRequest{Tenant: tenant, Kind: kind, Name: name, Version: version}
	if evidence != "" {
		req.Evaluation = payload(h.t, evidence)
	}
	resp, err := h.client.Promote(context.Background(), withToken(req, token))
	if err != nil {
		return nil, err
	}
	return resp.Msg, nil
}

func (h *harness) promote(tenant *commonv1.TenantContext, kind registryv1.RegistryKind, name, version, evidence string) (*registryv1.PromoteResponse, error) {
	return h.promoteAs(publishToken, tenant, kind, name, version, evidence)
}

// mustPromote 带达标的证据晋升，返回原来的默认版本。
func (h *harness) mustPromote(tenant *commonv1.TenantContext, kind registryv1.RegistryKind, name, version string) string {
	h.t.Helper()
	resp, err := h.promote(tenant, kind, name, version, passingEvidence)
	if err != nil {
		h.t.Fatalf("晋升 %s %s@%s 失败：%v", kind, name, version, err)
	}
	return resp.GetPreviousDefaultVersion()
}

// release 发布并设为默认，返回版本号。
func (h *harness) release(tenant *commonv1.TenantContext, kind registryv1.RegistryKind, name, doc string) string {
	h.t.Helper()
	version := h.mustPublish(tenant, kind, name, doc)
	h.mustPromote(tenant, kind, name, version)
	return version
}

// releaseN 连续发布 n 个版本（内容相同），把最后一个设为默认：用来造出 sql_query@v3 这类引用。
func (h *harness) releaseN(n int, name, doc string) string {
	h.t.Helper()
	var version string
	for range n {
		version = h.mustPublish(nil, kindTemplate, name, doc)
	}
	h.mustPromote(nil, kindTemplate, name, version)
	return version
}

func (h *harness) getTemplate(tenant *commonv1.TenantContext, name, version string) (*registryv1.NodeTemplate, error) {
	resp, err := h.client.GetNodeTemplate(context.Background(), connect.NewRequest(&registryv1.GetNodeTemplateRequest{Tenant: tenant, Name: name, Version: version}))
	if err != nil {
		return nil, err
	}
	return resp.Msg.GetNodeTemplate(), nil
}

func (h *harness) mustGetTemplate(tenant *commonv1.TenantContext, name, version string) *registryv1.NodeTemplate {
	h.t.Helper()
	got, err := h.getTemplate(tenant, name, version)
	if err != nil {
		h.t.Fatalf("读 %s@%q 失败：%v", name, version, err)
	}
	return got
}

// listTemplates 逐页取完 ListNodeTemplates。
func (h *harness) listTemplates(tenant *commonv1.TenantContext, pins *workflowv1.PinnedVersions, pageSize int32) ([]*registryv1.NodeTemplate, error) {
	var out []*registryv1.NodeTemplate
	token := ""
	for range 100 {
		resp, err := h.client.ListNodeTemplates(context.Background(), connect.NewRequest(&registryv1.ListNodeTemplatesRequest{
			Tenant: tenant, PinnedVersions: pins, PageSize: pageSize, PageToken: token,
		}))
		if err != nil {
			return nil, err
		}
		out = append(out, resp.Msg.GetNodeTemplates()...)
		if token = resp.Msg.GetNextPageToken(); token == "" {
			return out, nil
		}
	}
	h.t.Fatal("分页 100 次还没取完")
	return nil, nil
}

// listRecipes 逐页取完 ListRecipes。
func (h *harness) listRecipes(tenant *commonv1.TenantContext, family string) ([]*registryv1.Recipe, error) {
	var out []*registryv1.Recipe
	token := ""
	for range 100 {
		resp, err := h.client.ListRecipes(context.Background(), connect.NewRequest(&registryv1.ListRecipesRequest{
			Tenant: tenant, TaskFamily: family, PageToken: token,
		}))
		if err != nil {
			return nil, err
		}
		out = append(out, resp.Msg.GetRecipes()...)
		if token = resp.Msg.GetNextPageToken(); token == "" {
			return out, nil
		}
	}
	h.t.Fatal("分页 100 次还没取完")
	return nil, nil
}

func (h *harness) getRecipe(tenant *commonv1.TenantContext, name, version string) (*registryv1.Recipe, error) {
	resp, err := h.client.GetRecipe(context.Background(), connect.NewRequest(&registryv1.GetRecipeRequest{Tenant: tenant, Name: name, Version: version}))
	if err != nil {
		return nil, err
	}
	return resp.Msg.GetRecipe(), nil
}

func (h *harness) getPolicy(tenant *commonv1.TenantContext, version string) (*registryv1.TenantPolicy, error) {
	resp, err := h.client.GetTenantPolicy(context.Background(), connect.NewRequest(&registryv1.GetTenantPolicyRequest{Tenant: tenant, Version: version}))
	if err != nil {
		return nil, err
	}
	return resp.Msg.GetTenantPolicy(), nil
}

func (h *harness) resolve(tenant *commonv1.TenantContext, family, recipe string) (*workflowv1.PinnedVersions, error) {
	resp, err := h.client.ResolvePinnedVersions(context.Background(), connect.NewRequest(&registryv1.ResolvePinnedVersionsRequest{
		Tenant: tenant, TaskFamily: family, Recipe: recipe,
	}))
	if err != nil {
		return nil, err
	}
	return resp.Msg.GetPinnedVersions(), nil
}

func (h *harness) mustResolve(tenant *commonv1.TenantContext, family, recipe string) *workflowv1.PinnedVersions {
	h.t.Helper()
	pins, err := h.resolve(tenant, family, recipe)
	if err != nil {
		h.t.Fatalf("ResolvePinnedVersions 失败：%v", err)
	}
	return pins
}

// --- 断言 ---

// versions 把模板列表变成 名字 → 版本。
func versions(templates []*registryv1.NodeTemplate) map[string]string {
	out := map[string]string{}
	for _, t := range templates {
		out[t.GetName()] = t.GetVersion()
	}
	return out
}

func names(templates []*registryv1.NodeTemplate) []string {
	out := make([]string, 0, len(templates))
	for _, t := range templates {
		out = append(out, t.GetName())
	}
	return out
}

func recipeRefs(recipes []*registryv1.Recipe) []string {
	out := make([]string, 0, len(recipes))
	for _, r := range recipes {
		out = append(out, r.GetName()+"@"+r.GetVersion())
	}
	sort.Strings(out)
	return out
}

func wantCode(t *testing.T, what string, err error, code connect.Code) {
	t.Helper()
	if err == nil {
		t.Fatalf("%s：应返回 %v，实际成功", what, code)
	}
	if got := connect.CodeOf(err); got != code {
		t.Fatalf("%s：应返回 %v，实际 %v（%v）", what, code, got, err)
	}
}

// errorDetail 取出 trailer 里的 common.v1.Error。
func errorDetail(t *testing.T, err error) (connect.Code, *commonv1.Error) {
	t.Helper()
	if err == nil {
		t.Fatal("应返回错误")
	}
	var ce *connect.Error
	if !errors.As(err, &ce) {
		t.Fatalf("应是 *connect.Error，实际 %T", err)
	}
	values := ce.Meta().Values(errorTrailer)
	if len(values) == 0 {
		t.Fatalf("错误应带 %s trailer：%v", errorTrailer, err)
	}
	raw, decodeErr := connect.DecodeBinaryHeader(values[0])
	if decodeErr != nil {
		t.Fatalf("%s 不是合法的二进制 trailer：%v", errorTrailer, decodeErr)
	}
	var pe commonv1.Error
	if err := proto.Unmarshal(raw, &pe); err != nil {
		t.Fatalf("%s 不是序列化的 common.v1.Error：%v", errorTrailer, err)
	}
	return ce.Code(), &pe
}

// errorMessage 是返回给调用方的说明（不含状态码前缀）。
func errorMessage(err error) string {
	var ce *connect.Error
	if errors.As(err, &ce) {
		return ce.Message()
	}
	return err.Error()
}
