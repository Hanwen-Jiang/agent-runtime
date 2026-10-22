package taskapi

// T-08 验收：task-service 配了 Registry 时，新 Task 的工作流输入里是 Registry 在提交那一刻给出的固定版本
// （设计书 3.1.2“固定”；docs/plan/registry.md 第 2 节第 5 条）；之后的发布与晋升只影响新 Task。
// Registry 不可用时新 Task 的准入失败（fail-closed）：提交返回 Unavailable，不投递给 Session 工作流；
// Registry 恢复后，客户端用同一个 client_request_id 重试，照常投递。

import (
	"context"
	"path/filepath"
	"sync/atomic"
	"testing"

	"connectrpc.com/connect"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/structpb"

	"github.com/agentruntime/agent-runtime/libs/go-core/dev/filesecrets"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/sqlitestore"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	registryv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/registry/v1"
	workflowv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workflow/v1"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/registry"
)

const registryToken = "taskapi-test-publish-token"

const registryEvidence = `{"eval_set":{"name":"task-loop","version":"c0117be"},"samples":26,` +
	`"candidate":{"success_rate":0.92,"cost_per_success_usd":0.05},"passed":true,"report":"r.json","evaluated_at":"2026-10-22T09:00:00Z"}`

// flakyStore 包住 RelationalStore：down 时绑定作用域就报 transient（业务库或 Registry 的存储不可用）。
type flakyStore struct {
	relational.Provider
	down atomic.Bool
}

func (f *flakyStore) unavailable() error {
	return providererr.New(providererr.Transient, "store_unavailable", "业务库不可用")
}

func (f *flakyStore) Bind(t *commonv1.TenantContext) (relational.Store, error) {
	if f.down.Load() {
		return nil, f.unavailable()
	}
	return f.Provider.Bind(t)
}

func (f *flakyStore) BindPlatform(purpose string) (relational.Store, error) {
	if f.down.Load() {
		return nil, f.unavailable()
	}
	return f.Provider.BindPlatform(purpose)
}

// newRegistry 在单独的业务库上构造 Registry（与 taskapi 的业务库分开，才能单独让它不可用）。
func newRegistry(t *testing.T) (*registry.Registry, *flakyStore) {
	t.Helper()
	inner, err := sqlitestore.New(sqlitestore.Options{Dir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = inner.Close() })
	store := &flakyStore{Provider: inner}
	if err := store.Migrate(context.Background(), registry.Migrations()); err != nil {
		t.Fatal(err)
	}
	secrets, err := filesecrets.New(filesecrets.Options{Path: filepath.Join(t.TempDir(), "secrets.json")})
	if err != nil {
		t.Fatal(err)
	}
	secrets.SeedSecret("", registry.PublishTokenSecret, registryToken)
	reg, err := registry.New(registry.Options{Store: store, Secrets: secrets, SchemaDir: filepath.Join("..", "..", "..", "..", "schemas")})
	if err != nil {
		t.Fatal(err)
	}
	return reg, store
}

func registryPayload(t *testing.T, js string) *commonv1.JsonPayload {
	t.Helper()
	var v structpb.Value
	if err := protojson.Unmarshal([]byte(js), &v); err != nil {
		t.Fatal(err)
	}
	return &commonv1.JsonPayload{Value: &v}
}

// registryRelease 在进程内发布并设为默认（租户策略发布即生效），返回版本号。
func registryRelease(t *testing.T, reg *registry.Registry, tenant *commonv1.TenantContext, kind registryv1.RegistryKind, name, doc string) string {
	t.Helper()
	ctx := context.Background()
	pub := connect.NewRequest(&registryv1.PublishRequest{Tenant: tenant, Kind: kind, Name: name, Document: registryPayload(t, doc)})
	pub.Header().Set("Authorization", "Bearer "+registryToken)
	resp, err := reg.Publish(ctx, pub)
	if err != nil {
		t.Fatalf("发布 %s %s 失败：%v", kind, name, err)
	}
	version := resp.Msg.GetVersion()
	if kind == registryv1.RegistryKind_REGISTRY_KIND_TENANT_POLICY {
		return version
	}
	promote := connect.NewRequest(&registryv1.PromoteRequest{Tenant: tenant, Kind: kind, Name: name, Version: version, Evaluation: registryPayload(t, registryEvidence)})
	promote.Header().Set("Authorization", "Bearer "+registryToken)
	if _, err := reg.Promote(ctx, promote); err != nil {
		t.Fatalf("晋升 %s %s@%s 失败：%v", kind, name, version, err)
	}
	return version
}

const (
	pinAnswerDoc   = `{"description":"直接作答","executor":"model","effect":"none","role":"deliver","output":"generate"}`
	pinAnswerDocV2 = `{"description":"直接作答，先列要点","executor":"model","effect":"none","role":"deliver","output":"generate"}`
	pinProfileID   = "a6api/gpt-4o-mini@2026-10"
	pinProfileDoc  = `{"id":"a6api/gpt-4o-mini@2026-10","vendor":"openai","model":"gpt-4o-mini","class":"small",` +
		`"tiers":[{"name":"low"}],"capabilities":["generate"],"cost":{"in_per_m":0.15,"out_per_m":0.6,"cache_read_per_m":0.075},` +
		`"freshness":{"price_at":"2026-10-21"}}`
)

func TestSubmitPinsTheRegistryDefaultsAtSubmitTime(t *testing.T) {
	reg, _ := newRegistry(t)
	t1 := &commonv1.TenantContext{TenantId: "t1"}
	registryRelease(t, reg, nil, registryv1.RegistryKind_REGISTRY_KIND_NODE_TEMPLATE, "answer", pinAnswerDoc)
	registryRelease(t, reg, nil, registryv1.RegistryKind_REGISTRY_KIND_MODEL_PROFILE, pinProfileID, pinProfileDoc)
	registryRelease(t, reg, t1, registryv1.RegistryKind_REGISTRY_KIND_TENANT_POLICY, "", `{"effect_ceiling":"read"}`)

	f := newAPIWith(t, func(d *Deps) { d.Pins = registry.Pins{Registry: reg} })
	token := f.token("t1", "u1")

	f.mustSubmit(token, newRequest("first"))
	_, first := f.lastSubmit()
	want := &workflowv1.PinnedVersions{
		NodeTemplates: map[string]string{"answer": "v1"},
		ModelProfiles: map[string]string{pinProfileID: "v1"},
		TenantPolicy:  "v1",
	}
	if !proto.Equal(first.GetTask().GetPinnedVersions(), want) {
		t.Errorf("工作流输入里是 Registry 给的固定版本：\n得到 %v\n应为 %v", first.GetTask().GetPinnedVersions(), want)
	}

	registryRelease(t, reg, nil, registryv1.RegistryKind_REGISTRY_KIND_NODE_TEMPLATE, "answer", pinAnswerDocV2)
	f.mustSubmit(token, newRequest("second"))
	_, second := f.lastSubmit()
	if got := second.GetTask().GetPinnedVersions().GetNodeTemplates()["answer"]; got != "v2" {
		t.Errorf("晋升之后提交的 Task 固定新的默认版本 v2，实际 %q", got)
	}
	if got := first.GetTask().GetPinnedVersions().GetNodeTemplates()["answer"]; got != "v1" {
		t.Errorf("先提交的 Task 仍是 v1，实际 %q", got)
	}
}

func TestSubmitFailsClosedWhenTheRegistryIsUnavailable(t *testing.T) {
	reg, store := newRegistry(t)
	registryRelease(t, reg, nil, registryv1.RegistryKind_REGISTRY_KIND_NODE_TEMPLATE, "answer", pinAnswerDoc)
	f := newAPIWith(t, func(d *Deps) { d.Pins = registry.Pins{Registry: reg} })
	token := f.token("t1", "u1")

	store.down.Store(true)
	_, err := f.submit(token, newRequest("crid-1"))
	wantCode(t, "Registry 不可用时提交", err, connect.CodeUnavailable)
	if calls := f.durable.callsOf("SignalWithStart"); len(calls) != 0 {
		t.Fatalf("Registry 不可用时不启动工作流，实际投递了 %d 次", len(calls))
	}

	store.down.Store(false)
	resp := f.mustSubmit(token, newRequest("crid-1"))
	_, sig := f.lastSubmit()
	if got := sig.GetTask().GetPinnedVersions().GetNodeTemplates()["answer"]; got != "v1" {
		t.Errorf("恢复后重试同一提交，按 Registry 固定版本投递：%v", sig.GetTask().GetPinnedVersions())
	}
	if n := len(f.durable.callsOf("SignalWithStart")); n != 1 {
		t.Errorf("恢复后只投递一次，实际 %d 次", n)
	}
	if !taskPattern.MatchString(resp.GetTaskId()) {
		t.Errorf("返回 task_id：%q", resp.GetTaskId())
	}
}
