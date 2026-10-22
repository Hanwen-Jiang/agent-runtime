package main

// T-08 验收：开发种子（docs/plan/registry.md 第 2 节第 8 条）。task-service registry-seed <文件> 把一批文档发布并设为默认，
// 供本地与端到端测试使用。它直接写业务库（与 serve 同一处：AR_DATA_DIR 下的 SQLite，或 AR_RELATIONAL_DSN），
// 不需要 Temporal 与签名密钥，也不走发布令牌。文件格式：
//
//	{"entries": [{"kind": "node_template", "name": "answer", "document": {...}},
//	             {"kind": "tenant_policy", "tenant": "t1", "document": {...}}, ...]}
//
// kind 取 node_template、recipe、model_profile、tenant_policy；tenant 只用于租户作用域的 Recipe 与租户策略；按顺序发布
// （Recipe 引用的模板要写在它前面）。文档先全部按 schema 校验，有一条不合格就一条也不写，退出码 1。
// 重复运行是幂等的：与当前默认版本内容相同的条目跳过；内容变了就发布新版本并设为默认。用法错误退出码 2。

import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"connectrpc.com/connect"

	"github.com/agentruntime/agent-runtime/libs/go-core/dev/filesecrets"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/sqlitestore"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	registryv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/registry/v1"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/registry"
)

var schemasDir = filepath.Join("..", "..", "..", "..", "schemas")

const seedFile = `{"entries": [
  {"kind": "node_template", "name": "answer",
   "document": {"description": "直接作答", "executor": "model", "effect": "none", "role": "deliver", "output": "generate"}},
  {"kind": "node_template", "name": "ledger_delete",
   "document": {"description": "删除台账条目", "executor": "program", "effect": "write_irreversible", "role": "act"}},
  {"kind": "node_template", "name": "ledger_write",
   "document": {"description": "写入台账", "executor": "program", "effect": "write_reversible", "role": "act", "rollback_template": "ledger_delete"}},
  {"kind": "model_profile", "name": "mock/model@2026-10",
   "document": {"id": "mock/model@2026-10", "vendor": "mock", "model": "model", "class": "mid", "tiers": [{"name": "low"}],
                "capabilities": ["generate"], "cost": {"in_per_m": 0, "out_per_m": 0, "cache_read_per_m": 0}, "freshness": {"price_at": "2026-10-22"}}},
  {"kind": "recipe", "name": "qa",
   "document": {"task_family": "general.qa", "description": "直接作答", "risk": "none",
                "stages": [{"goal": "作答", "exit_when": "给出答案", "nodes": [{"id": "answer", "kind": "atomic", "template": "answer@v1"}]}]}},
  {"kind": "recipe", "tenant": "t1", "name": "t1_books",
   "document": {"task_family": "finance.close", "description": "结账", "risk": "write_reversible",
                "stages": [{"goal": "记账", "exit_when": "记完", "nodes": [{"id": "write", "kind": "atomic", "template": "ledger_write@v1"}]}]}},
  {"kind": "tenant_policy", "tenant": "t1", "document": {"effect_ceiling": "write_reversible"}}
]}`

func writeSeed(t *testing.T, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "seed.json")
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

func seedEnv(dataDir string) func(string) string {
	return getenv(map[string]string{"AR_DATA_DIR": dataDir, "AR_SCHEMAS_DIR": schemasDir})
}

// openSeeded 在 serve 用的同一个业务库上构造 Registry，读回种下的内容。
func openSeeded(t *testing.T, dataDir string) *registry.Registry {
	t.Helper()
	store, err := sqlitestore.New(sqlitestore.Options{Dir: filepath.Join(dataDir, "relational")})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = store.Close() })
	if err := store.Migrate(context.Background(), registry.Migrations()); err != nil {
		t.Fatal(err)
	}
	secrets, err := filesecrets.New(filesecrets.Options{Path: filepath.Join(t.TempDir(), "secrets.json")})
	if err != nil {
		t.Fatal(err)
	}
	reg, err := registry.New(registry.Options{Store: store, Secrets: secrets, SchemaDir: schemasDir})
	if err != nil {
		t.Fatal(err)
	}
	return reg
}

func defaultTemplate(t *testing.T, reg *registry.Registry, name string) (*registryv1.NodeTemplate, error) {
	t.Helper()
	resp, err := reg.GetNodeTemplate(context.Background(), connect.NewRequest(&registryv1.GetNodeTemplateRequest{
		Tenant: &commonv1.TenantContext{TenantId: "t1"}, Name: name,
	}))
	if err != nil {
		return nil, err
	}
	return resp.Msg.GetNodeTemplate(), nil
}

func TestRegistrySeedPublishesAndSetsDefaults(t *testing.T) {
	dataDir := t.TempDir()
	var out, errOut bytes.Buffer
	if code := run([]string{"registry-seed", writeSeed(t, seedFile)}, &out, &errOut, seedEnv(dataDir)); code != 0 {
		t.Fatalf("registry-seed 应成功，退出码 %d：%s", code, errOut.String())
	}
	reg := openSeeded(t, dataDir)
	t1 := &commonv1.TenantContext{TenantId: "t1"}
	pins, err := reg.ResolvePinnedVersions(context.Background(), connect.NewRequest(&registryv1.ResolvePinnedVersionsRequest{Tenant: t1, TaskFamily: "general.qa"}))
	if err != nil {
		t.Fatal(err)
	}
	got := pins.Msg.GetPinnedVersions()
	if got.GetNodeTemplates()["answer"] != "v1" || got.GetNodeTemplates()["ledger_write"] != "v1" ||
		got.GetModelProfiles()["mock/model@2026-10"] != "v1" || got.GetRecipe() != "qa@v1" || got.GetTenantPolicy() != "v1" {
		t.Errorf("种下的条目都是默认版本：%v", got)
	}
	recipe, err := reg.GetRecipe(context.Background(), connect.NewRequest(&registryv1.GetRecipeRequest{Tenant: t1, Name: "t1_books"}))
	if err != nil || recipe.Msg.GetRecipe().GetState() != registryv1.ReleaseState_RELEASE_STATE_DEFAULT {
		t.Errorf("租户作用域的 Recipe 种在 t1 下：%v", err)
	}
	_, err = reg.GetRecipe(context.Background(), connect.NewRequest(&registryv1.GetRecipeRequest{Tenant: &commonv1.TenantContext{TenantId: "t2"}, Name: "t1_books"}))
	if connect.CodeOf(err) != connect.CodeNotFound {
		t.Errorf("t2 看不到 t1 的 Recipe：%v", err)
	}
}

func TestRegistrySeedIsIdempotent(t *testing.T) {
	dataDir := t.TempDir()
	path := writeSeed(t, seedFile)
	for i := range 2 {
		var out, errOut bytes.Buffer
		if code := run([]string{"registry-seed", path}, &out, &errOut, seedEnv(dataDir)); code != 0 {
			t.Fatalf("第 %d 次 registry-seed 应成功，退出码 %d：%s", i+1, code, errOut.String())
		}
	}
	reg := openSeeded(t, dataDir)
	answer, err := defaultTemplate(t, reg, "answer")
	if err != nil || answer.GetVersion() != "v1" {
		t.Errorf("重复运行不发布新版本：%+v（%v）", answer, err)
	}

	changed := strings.Replace(seedFile, `"description": "直接作答", "executor"`, `"description": "直接作答，先列要点", "executor"`, 1)
	var out, errOut bytes.Buffer
	if code := run([]string{"registry-seed", writeSeed(t, changed)}, &out, &errOut, seedEnv(dataDir)); code != 0 {
		t.Fatalf("内容变了的 registry-seed 应成功，退出码 %d：%s", code, errOut.String())
	}
	answer, err = defaultTemplate(t, reg, "answer")
	if err != nil || answer.GetVersion() != "v2" {
		t.Errorf("内容变了就发布新版本并设为默认：%+v（%v）", answer, err)
	}
}

func TestRegistrySeedWritesNothingWhenAnEntryIsInvalid(t *testing.T) {
	dataDir := t.TempDir()
	bad := strings.Replace(seedFile, `"effect": "write_irreversible", "role": "act"`, `"effect": "write_irreversible", "role": "fetch", "output": "generate"`, 1)
	var out, errOut bytes.Buffer
	if code := run([]string{"registry-seed", writeSeed(t, bad)}, &out, &errOut, seedEnv(dataDir)); code != 1 {
		t.Fatalf("有不合格的条目时退出码 1，实际 %d", code)
	}
	if !strings.Contains(errOut.String(), "ledger_delete") {
		t.Errorf("错误信息指出哪一条不合格：%s", errOut.String())
	}
	reg := openSeeded(t, dataDir)
	if _, err := defaultTemplate(t, reg, "answer"); connect.CodeOf(err) != connect.CodeNotFound {
		t.Errorf("排在前面的合格条目也没有写入：%v", err)
	}
}

func TestRegistrySeedUsage(t *testing.T) {
	var out, errOut bytes.Buffer
	if code := run([]string{"registry-seed"}, &out, &errOut, seedEnv(t.TempDir())); code != 2 {
		t.Errorf("没有给文件时退出码 2，实际 %d", code)
	}
	if code := run([]string{"registry-seed", filepath.Join(t.TempDir(), "missing.json")}, &out, &errOut, seedEnv(t.TempDir())); code != 1 {
		t.Errorf("文件读不到时退出码 1，实际 %d", code)
	}
	if code := run([]string{"registry-seed", writeSeed(t, `{"entries": [{"kind": "widget", "name": "x", "document": {}}]}`)}, &out, &errOut, seedEnv(t.TempDir())); code != 1 {
		t.Errorf("不认识的 kind 退出码 1，实际 %d", code)
	}
}
