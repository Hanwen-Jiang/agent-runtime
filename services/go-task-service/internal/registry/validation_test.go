package registry_test

// T-08 验收：发布时的校验（docs/plan/registry.md 第 3 节）。document 按种类对应的 /schemas 校验：
// schemas/examples 里的正例都能发布，反例都被拒（invalid）；JSON Schema 表达不了的由 Registry 用代码检查：
// 名字的格式、Model Profile 的 id 等于发布名、Tier 名不重复、rollback_template 已发布、Recipe 引用的模板版本存在、
// risk 不低于各节点的 effect、Recipe 节点的 cache 与 retry 对照模板的 effect 符合附录 D.4、Node id 唯一、after 引用存在且无环。

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"connectrpc.com/connect"

	registryv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/registry/v1"
)

const schemaIDPrefix = "https://agentruntime.example/schemas/"

type example struct {
	Name     string          `json:"name"`
	Instance json.RawMessage `json:"instance"`
}

type exampleFile struct {
	Schema  string    `json:"schema"`
	Valid   []example `json:"valid"`
	Invalid []example `json:"invalid"`
}

func loadExamples(t *testing.T, schema string) exampleFile {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(examplesDir, schema+".examples.json"))
	if err != nil {
		t.Fatal(err)
	}
	var f exampleFile
	if err := json.Unmarshal(data, &f); err != nil {
		t.Fatal(err)
	}
	if len(f.Valid) == 0 || len(f.Invalid) == 0 {
		t.Fatalf("%s 的样例应有正例与反例", schema)
	}
	return f
}

// --- 按 schema 校验：用 S-01 的正反例 ---

func TestNodeTemplateExamples(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		// 正例 repo_edit_reversible 的回滚模板。
		h.release(nil, kindTemplate, "repo_revert", `{"description":"撤销仓库改动","executor":"program","effect":"none","role":"transform"}`)
		f := loadExamples(t, "node_template")
		for _, e := range f.Valid {
			if _, err := h.publish(nil, kindTemplate, e.Name, string(e.Instance)); err != nil {
				t.Errorf("正例 %s 应能发布：%v", e.Name, err)
			}
		}
		for _, e := range f.Invalid {
			_, err := h.publish(nil, kindTemplate, e.Name, string(e.Instance))
			wantCode(t, "反例 "+e.Name, err, connect.CodeInvalidArgument)
		}
	})
}

func TestRecipeExamples(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		// 正例引用的模板与版本：sql_query@v3、refund_attribution@v2、synthesize_report@v1、read_ci_log@v1。
		h.releaseN(3, "sql_query", sqlQueryDoc)
		h.releaseN(2, "refund_attribution", `{"description":"退款原因归因","executor":"model","effect":"none","role":"transform","output":"generate"}`)
		h.release(nil, kindTemplate, "synthesize_report", `{"description":"综合成稿","executor":"model","effect":"none","role":"deliver","output":"generate"}`)
		h.release(nil, kindTemplate, "read_ci_log", `{"description":"读取 CI 日志","executor":"program","effect":"read","role":"fetch"}`)
		f := loadExamples(t, "recipe")
		for _, e := range f.Valid {
			if _, err := h.publish(nil, kindRecipe, e.Name, string(e.Instance)); err != nil {
				t.Errorf("正例 %s 应能发布：%v", e.Name, err)
			}
		}
		for _, e := range f.Invalid {
			_, err := h.publish(nil, kindRecipe, e.Name, string(e.Instance))
			wantCode(t, "反例 "+e.Name, err, connect.CodeInvalidArgument)
		}
	})
}

func TestModelProfileExamples(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		f := loadExamples(t, "model_profile")
		for _, e := range f.Valid {
			var doc struct{ ID string }
			if err := json.Unmarshal(e.Instance, &doc); err != nil {
				t.Fatal(err)
			}
			if _, err := h.publish(nil, kindProfile, doc.ID, string(e.Instance)); err != nil {
				t.Errorf("正例 %s 应能发布：%v", e.Name, err)
			}
		}
		for _, e := range f.Invalid {
			_, err := h.publish(nil, kindProfile, profileID, string(e.Instance))
			wantCode(t, "反例 "+e.Name, err, connect.CodeInvalidArgument)
		}
	})
}

func TestTenantPolicyExamples(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		f := loadExamples(t, "tenant_policy")
		for _, e := range f.Valid {
			if _, err := h.publish(t1, kindPolicy, "", string(e.Instance)); err != nil {
				t.Errorf("正例 %s 应能发布：%v", e.Name, err)
			}
		}
		for _, e := range f.Invalid {
			_, err := h.publish(t1, kindPolicy, "", string(e.Instance))
			wantCode(t, "反例 "+e.Name, err, connect.CodeInvalidArgument)
		}
	})
}

func TestEvaluationExamples(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		f := loadExamples(t, "registry_evaluation")
		for _, e := range f.Valid {
			version := h.mustPublish(nil, kindTemplate, "answer", answerDoc)
			var doc struct{ Passed bool }
			if err := json.Unmarshal(e.Instance, &doc); err != nil {
				t.Fatal(err)
			}
			_, err := h.promote(nil, kindTemplate, "answer", version, string(e.Instance))
			switch {
			case doc.Passed && err != nil:
				t.Errorf("达标的证据 %s 应能晋升：%v", e.Name, err)
			case !doc.Passed:
				wantCode(t, "不达标的证据 "+e.Name, err, connect.CodeInvalidArgument)
			}
		}
		version := h.mustPublish(nil, kindTemplate, "answer", answerDoc)
		for _, e := range f.Invalid {
			_, err := h.promote(nil, kindTemplate, "answer", version, string(e.Instance))
			wantCode(t, "反例 "+e.Name, err, connect.CodeInvalidArgument)
		}
		if got := h.mustGetTemplate(t1, "answer", version); got.GetState() != stateShadow {
			t.Errorf("证据都不合格时候选仍是影子：%s", got.GetState())
		}
	})
}

func TestDocumentSchemaIDMustMatchTheKind(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		doc := payload(t, answerDoc)
		doc.SchemaId = schemaIDPrefix + "recipe.schema.json"
		_, err := h.client.Publish(t.Context(), withToken(&registryv1.PublishRequest{Kind: kindTemplate, Name: "answer", Document: doc}, publishToken))
		wantCode(t, "Node Template 的 document 声明成 Recipe", err, connect.CodeInvalidArgument)

		doc.SchemaId = schemaIDPrefix + "node_template.schema.json"
		if _, err := h.client.Publish(t.Context(), withToken(&registryv1.PublishRequest{Kind: kindTemplate, Name: "answer", Document: doc}, publishToken)); err != nil {
			t.Fatalf("schema_id 与种类一致时可以发布：%v", err)
		}
		h.mustPromote(nil, kindTemplate, "answer", "v1")
		h.release(nil, kindRecipe, "refund_analysis", recipeDoc(t, "analytics.attribution", "none", "answer@v1"))
		got, err := h.getRecipe(t1, "refund_analysis", "")
		if err != nil {
			t.Fatal(err)
		}
		if id := got.GetDocument().GetSchemaId(); id != schemaIDPrefix+"recipe.schema.json" {
			t.Errorf("读出的 document 带上种类对应的 schema $id，实际 %q", id)
		}
	})
}

// --- 代码检查 ---

func TestNamesFollowTheRefFormat(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		for _, name := range []string{"", "Answer", "answer@v1", "1st", "an-swer", "a b"} {
			_, err := h.publish(nil, kindTemplate, name, answerDoc)
			wantCode(t, "模板名 "+name, err, connect.CodeInvalidArgument)
		}
		_, err := h.publish(t1, kindPolicy, "policy", policyDoc)
		wantCode(t, "租户策略不起名字", err, connect.CodeInvalidArgument)
	})
}

func TestModelProfileNameMustEqualDocumentID(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		_, err := h.publish(nil, kindProfile, "a6api/other-model@2026-10", profileDoc)
		wantCode(t, "发布名与 document.id 不一致", err, connect.CodeInvalidArgument)
	})
}

func TestTierNamesAreUniqueWithinAProfile(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		var doc map[string]any
		if err := json.Unmarshal([]byte(profileDoc), &doc); err != nil {
			t.Fatal(err)
		}
		doc["tiers"] = []any{map[string]any{"name": "low"}, map[string]any{"name": "low", "effort": "high"}}
		_, err := h.publish(nil, kindProfile, profileID, mustJSON(t, doc))
		wantCode(t, "Tier 名重复", err, connect.CodeInvalidArgument)
	})
}

func TestRollbackTemplateMustBePublished(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		_, err := h.publish(nil, kindTemplate, "ledger_write", ledgerWriteDoc)
		wantCode(t, "回滚模板 ledger_delete 还没发布", err, connect.CodeInvalidArgument)
		h.mustPublish(nil, kindTemplate, "ledger_delete", ledgerDeleteDoc) // 影子也算已发布
		if _, err := h.publish(nil, kindTemplate, "ledger_write", ledgerWriteDoc); err != nil {
			t.Errorf("回滚模板发布之后可以发布：%v", err)
		}
	})
}

func TestRecipeReferencesAreChecked(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		h.release(nil, kindTemplate, "answer", answerDoc)
		h.release(nil, kindTemplate, "sql_query", sqlQueryDoc)
		h.release(nil, kindTemplate, "ledger_delete", ledgerDeleteDoc)
		h.release(nil, kindTemplate, "ledger_write", ledgerWriteDoc)

		stage := func(nodes ...map[string]any) []any {
			return []any{map[string]any{"goal": "完成", "exit_when": "完成", "nodes": nodes}}
		}
		recipe := func(risk string, stages []any) string {
			return mustJSON(t, map[string]any{"task_family": "analytics.attribution", "description": "测试", "risk": risk, "stages": stages})
		}
		atomic := func(id, template string, extra map[string]any) map[string]any {
			node := map[string]any{"id": id, "kind": "atomic", "template": template}
			for k, v := range extra {
				node[k] = v
			}
			return node
		}
		goal := map[string]any{
			"id": "fix", "kind": "goal", "goal": "修复", "done_when": "通过", "mode": "tool_loop",
			"output_schema": map[string]any{"type": "object"}, "effect_ceiling": "write_reversible",
		}
		cases := map[string]string{
			"引用的模板不存在":              recipe("read", stage(atomic("q", "ghost@v1", nil))),
			"引用的模板版本不存在":            recipe("read", stage(atomic("q", "sql_query@v4", nil))),
			"risk 低于模板的 effect":     recipe("none", stage(atomic("q", "sql_query@v1", nil))),
			"risk 低于 Goal Node 的上限": recipe("read", stage(goal)),
			"Node id 重复":            recipe("read", append(stage(atomic("q", "sql_query@v1", nil)), stage(atomic("q", "answer@v1", nil))...)),
			"after 引用不存在":           recipe("read", stage(atomic("q", "sql_query@v1", map[string]any{"after": []string{"ghost"}}))),
			"after 引用别的 Stage":      recipe("read", append(stage(atomic("q", "sql_query@v1", nil)), stage(atomic("a", "answer@v1", map[string]any{"after": []string{"q"}}))...)),
			"after 成环": recipe("read", stage(
				atomic("a", "answer@v1", map[string]any{"after": []string{"b"}}),
				atomic("b", "sql_query@v1", map[string]any{"after": []string{"a"}}),
			)),
			"写操作的节点设缓存":                recipe("write_reversible", stage(atomic("w", "ledger_write@v1", map[string]any{"cache": map[string]any{"ttl_s": 60}}))),
			"write_irreversible 的节点重试": recipe("write_irreversible", stage(atomic("d", "ledger_delete@v1", map[string]any{"retry": map[string]any{"max": 2}}))),
			"read 的节点缓存不设 TTL":         recipe("read", stage(atomic("q", "sql_query@v1", map[string]any{"cache": map[string]any{}}))),
		}
		for name, doc := range cases {
			t.Run(name, func(t *testing.T) {
				_, err := h.publish(nil, kindRecipe, "checked", doc)
				wantCode(t, name, err, connect.CodeInvalidArgument)
			})
		}
		ok := recipe("write_reversible", stage(
			atomic("q", "sql_query@v1", map[string]any{"cache": map[string]any{"ttl_s": 60}}),
			atomic("w", "ledger_write@v1", map[string]any{"after": []string{"q"}}),
		))
		if _, err := h.publish(nil, kindRecipe, "checked", ok); err != nil {
			t.Errorf("合规的 Recipe 可以发布：%v", err)
		}
	})
}

func TestRecipeMetadataComesFromTheDocument(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		h.release(nil, kindTemplate, "answer", answerDoc)
		h.release(nil, kindRecipe, "refund_analysis", recipeDoc(t, "analytics.attribution", "none", "answer@v1"))
		got, err := h.getRecipe(t1, "refund_analysis", "")
		if err != nil {
			t.Fatal(err)
		}
		if got.GetTaskFamily() != "analytics.attribution" || got.GetState() != stateDefault || got.GetVersion() != "v1" {
			t.Errorf("Recipe 的任务族取自文档：%+v", got)
		}
	})
}
