package registry_test

// T-08 验收：版本、发布状态、晋升、固定与作用域（设计书 3.1.2；docs/plan/registry.md 第 2、4、5 节）。
//
// 规则：同一作用域、种类、名字下按发布顺序编号 v1、v2……；新版本是影子；Promote 把一个版本设为默认，原默认退役；
// 已发布的内容永不修改，退役的版本仍可按版本号读到。默认查询与 ResolvePinnedVersions 只看默认版本；按固定的版本
// 查询不受之后的发布与晋升影响。平台作用域的条目所有租户可读，租户作用域的只有该租户可读；租户的 Recipe 不得与平台的同名。
// 读请求缺租户是 denied（与 S-02 一致）。租户策略没有影子：发布即生效（proto 的 TenantPolicy 没有发布状态），不走 Promote。

import (
	"context"
	"fmt"
	"slices"
	"sync"
	"testing"

	"connectrpc.com/connect"
	"google.golang.org/protobuf/proto"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/postgres"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	registryv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/registry/v1"
	workflowv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workflow/v1"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/registry"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/taskstore"
)

// --- 版本与发布状态 ---

func TestPublishNumbersVersionsAndStartsAsShadow(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		for i, doc := range []string{answerDoc, answerDocV2} {
			resp, err := h.publish(nil, kindTemplate, "answer", doc)
			if err != nil {
				t.Fatal(err)
			}
			want := fmt.Sprintf("v%d", i+1)
			if resp.GetName() != "answer" || resp.GetVersion() != want || resp.GetState() != stateShadow {
				t.Errorf("第 %d 次发布应得到 answer %s SHADOW，实际 %+v", i+1, want, resp)
			}
		}
		if v := h.mustPublish(nil, kindTemplate, "sql_query", sqlQueryDoc); v != "v1" {
			t.Errorf("另一个名字从 v1 编号，实际 %s", v)
		}
		got := h.mustGetTemplate(t1, "answer", "v1")
		if got.GetState() != stateShadow || got.GetVersion() != "v1" {
			t.Errorf("按版本号读得到影子版本：%+v", got)
		}
	})
}

func TestShadowVersionsDoNotLeak(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		h.release(nil, kindTemplate, "sql_query", sqlQueryDoc)
		h.mustPublish(nil, kindTemplate, "answer", answerDoc) // 只有影子

		_, err := h.getTemplate(t1, "answer", "")
		wantCode(t, "只有影子版本时，默认查询", err, connect.CodeNotFound)
		listed, err := h.listTemplates(t1, nil, 0)
		if err != nil {
			t.Fatal(err)
		}
		if got := versions(listed); len(got) != 1 || got["sql_query"] != "v1" {
			t.Errorf("默认列表只含默认版本，实际 %v", got)
		}
		pins := h.mustResolve(t1, "", "")
		if _, ok := pins.GetNodeTemplates()["answer"]; ok {
			t.Errorf("ResolvePinnedVersions 不固定影子版本：%v", pins.GetNodeTemplates())
		}
	})
}

func TestPromoteSetsDefaultAndRetiresThePrevious(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		v1 := h.mustPublish(nil, kindTemplate, "answer", answerDoc)
		v2 := h.mustPublish(nil, kindTemplate, "answer", answerDocV2)
		if prev := h.mustPromote(nil, kindTemplate, "answer", v1); prev != "" {
			t.Errorf("第一次晋升没有原默认版本，实际 %q", prev)
		}
		if prev := h.mustPromote(nil, kindTemplate, "answer", v2); prev != v1 {
			t.Errorf("晋升 v2 应返回原默认 v1，实际 %q", prev)
		}
		if got := h.mustGetTemplate(t1, "answer", ""); got.GetVersion() != v2 || got.GetState() != stateDefault {
			t.Errorf("默认查询应得到 v2 DEFAULT，实际 %s %s", got.GetVersion(), got.GetState())
		}
		if got := h.mustGetTemplate(t1, "answer", v1); got.GetState() != stateRetired {
			t.Errorf("原默认版本退役，仍按版本号读得到：%s", got.GetState())
		}
	})
}

func TestPromoteCanRollBackToARetiredVersion(t *testing.T) {
	// 附录 J.3：指标变差就回滚到上一版本，即把退役的版本重新设为默认。
	eachBackend(t, func(t *testing.T, h *harness) {
		v1 := h.release(nil, kindTemplate, "answer", answerDoc)
		v2 := h.release(nil, kindTemplate, "answer", answerDocV2)
		if prev := h.mustPromote(nil, kindTemplate, "answer", v1); prev != v2 {
			t.Errorf("回滚到 v1 应返回原默认 v2，实际 %q", prev)
		}
		if got := h.mustGetTemplate(t1, "answer", ""); got.GetVersion() != v1 {
			t.Errorf("回滚后默认是 v1，实际 %s", got.GetVersion())
		}
		if got := h.mustGetTemplate(t1, "answer", v2); got.GetState() != stateRetired {
			t.Errorf("回滚后 v2 退役，实际 %s", got.GetState())
		}
	})
}

func TestPublishedContentNeverChanges(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		h.release(nil, kindTemplate, "answer", answerDoc)
		first := recipeDoc(t, "analytics.attribution", "none", "answer@v1")
		v := h.mustPublish(nil, kindRecipe, "refund_analysis", first)
		h.release(nil, kindRecipe, "refund_analysis", recipeDoc(t, "analytics.attribution", "none", "answer@v1", "answer@v1"))
		got, err := h.getRecipe(t1, "refund_analysis", v)
		if err != nil {
			t.Fatal(err)
		}
		if !proto.Equal(got.GetDocument().GetValue(), value(t, first)) {
			t.Errorf("已发布的版本内容不变：\n得到 %v\n应为 %s", got.GetDocument().GetValue(), first)
		}
	})
}

func TestConcurrentPublishesGetDistinctVersions(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		const n = 8
		var wg sync.WaitGroup
		got := make([]string, n)
		errs := make([]error, n)
		for i := range n {
			wg.Add(1)
			go func() {
				defer wg.Done()
				resp, err := h.publish(nil, kindTemplate, "answer", answerDoc)
				errs[i] = err
				got[i] = resp.GetVersion()
			}()
		}
		wg.Wait()
		for i, err := range errs {
			if err != nil {
				t.Fatalf("并发发布第 %d 个失败（版本号冲突应在内部重试）：%v", i, err)
			}
		}
		slices.Sort(got)
		want := []string{"v1", "v2", "v3", "v4", "v5", "v6", "v7", "v8"}
		slices.Sort(want)
		if !slices.Equal(got, want) {
			t.Errorf("并发发布的版本号不重复、不跳号：%v", got)
		}
	})
}

func TestNodeTemplateFieldsComeFromTheDocument(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		h.release(nil, kindTemplate, "ledger_delete", ledgerDeleteDoc)
		h.release(nil, kindTemplate, "ledger_write", ledgerWriteDoc)
		h.release(nil, kindTemplate, "sql_query", sqlQueryDoc)
		h.release(nil, kindTemplate, "judge", judgeDoc)

		write := h.mustGetTemplate(t1, "ledger_write", "")
		if write.GetExecutor() != commonv1.Executor_EXECUTOR_PROGRAM || write.GetEffect() != commonv1.Effect_EFFECT_WRITE_REVERSIBLE ||
			write.GetRole() != commonv1.Role_ROLE_ACT || write.GetRollbackTemplate() != "ledger_delete" || !write.GetSupportsIdempotencyLookup() {
			t.Errorf("ledger_write 的骨架字段取自文档：%+v", write)
		}
		if write.GetOutput() != commonv1.ModelOutput_MODEL_OUTPUT_UNSPECIFIED {
			t.Errorf("program 模板没有 output：%s", write.GetOutput())
		}

		judge := h.mustGetTemplate(t1, "judge", "")
		if judge.GetExecutor() != commonv1.Executor_EXECUTOR_MODEL || judge.GetOutput() != commonv1.ModelOutput_MODEL_OUTPUT_SCORE ||
			judge.GetRole() != commonv1.Role_ROLE_JUDGE || judge.GetEffect() != commonv1.Effect_EFFECT_NONE {
			t.Errorf("judge 的骨架字段取自文档：%+v", judge)
		}

		sql := h.mustGetTemplate(t1, "sql_query", "")
		wantParams := value(t, `{"type":"object","required":["sql"],"properties":{"sql":{"type":"string"}}}`)
		if !proto.Equal(sql.GetParamsSchema().GetValue(), wantParams) {
			t.Errorf("params_schema 原样带出：%v", sql.GetParamsSchema().GetValue())
		}
		wantDefaults := value(t, `{"cache":{"ttl_s":600},"retry":{"max":2}}`).GetStructValue()
		if !proto.Equal(sql.GetDefaults(), wantDefaults) {
			t.Errorf("defaults 原样带出：%v", sql.GetDefaults())
		}
	})
}

// --- 晋升要证据 ---

func TestPromoteRequiresPassingEvidence(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		v1 := h.release(nil, kindTemplate, "answer", answerDoc)
		v2 := h.mustPublish(nil, kindTemplate, "answer", answerDocV2)
		cases := map[string]string{
			"没有证据":        "",
			"证据不合 schema": `{"eval_set":{"name":"task-loop"},"samples":0,"passed":true}`,
			"结论不通过":       failingEvidence,
		}
		for name, evidence := range cases {
			t.Run(name, func(t *testing.T) {
				_, err := h.promote(nil, kindTemplate, "answer", v2, evidence)
				wantCode(t, name, err, connect.CodeInvalidArgument)
				if got := h.mustGetTemplate(t1, "answer", ""); got.GetVersion() != v1 {
					t.Errorf("拒绝晋升时默认版本不变，实际 %s", got.GetVersion())
				}
				if got := h.mustGetTemplate(t1, "answer", v2); got.GetState() != stateShadow {
					t.Errorf("拒绝晋升时候选仍是影子，实际 %s", got.GetState())
				}
			})
		}
	})
}

func TestPromoteUnknownVersionIsNotFound(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		h.release(nil, kindTemplate, "answer", answerDoc)
		_, err := h.promote(nil, kindTemplate, "answer", "v9", passingEvidence)
		wantCode(t, "晋升不存在的版本", err, connect.CodeNotFound)
		_, err = h.promote(nil, kindTemplate, "nope", "v1", passingEvidence)
		wantCode(t, "晋升不存在的名字", err, connect.CodeNotFound)
	})
}

func TestTenantPolicyTakesEffectOnPublish(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		resp, err := h.publish(t1, kindPolicy, "", policyDoc)
		if err != nil {
			t.Fatal(err)
		}
		if resp.GetVersion() != "v1" || resp.GetState() != stateDefault {
			t.Errorf("租户策略发布即生效：%+v", resp)
		}
		if _, err := h.publish(t1, kindPolicy, "", policyDocV2); err != nil {
			t.Fatal(err)
		}
		current, err := h.getPolicy(t1, "")
		if err != nil {
			t.Fatal(err)
		}
		if current.GetVersion() != "v2" || !proto.Equal(current.GetDocument().GetValue(), value(t, policyDocV2)) {
			t.Errorf("当前租户策略是最新发布的 v2：%+v", current)
		}
		old, err := h.getPolicy(t1, "v1")
		if err != nil || !proto.Equal(old.GetDocument().GetValue(), value(t, policyDoc)) {
			t.Errorf("旧版本仍按版本号读得到（已固定它的 Task 要用）：%+v（%v）", old, err)
		}
		_, err = h.promote(t1, kindPolicy, "", "v1", passingEvidence)
		wantCode(t, "租户策略不走 Promote", err, connect.CodeInvalidArgument)
	})
}

// --- 固定 ---

func TestResolvePinnedVersionsPinsOnlyDefaults(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		h.release(nil, kindTemplate, "answer", answerDoc)
		h.mustPublish(nil, kindTemplate, "answer", answerDocV2) // 影子
		h.release(nil, kindTemplate, "sql_query", sqlQueryDoc)
		h.release(nil, kindProfile, profileID, profileDoc)
		h.mustPublish(t1, kindPolicy, "", policyDoc)

		pins := h.mustResolve(t1, "", "")
		want := &workflowv1.PinnedVersions{
			NodeTemplates: map[string]string{"answer": "v1", "sql_query": "v1"},
			ModelProfiles: map[string]string{profileID: "v1"},
			TenantPolicy:  "v1",
		}
		if !proto.Equal(pins, want) {
			t.Errorf("只固定默认版本与当前的租户策略：\n得到 %v\n应为 %v", pins, want)
		}
		if len(pins.GetReasoningTemplates()) != 0 {
			t.Errorf("第一期没有 Reasoning Template：%v", pins.GetReasoningTemplates())
		}
	})
}

func TestPinnedQueriesIgnoreLaterReleases(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		h.release(nil, kindTemplate, "answer", answerDoc)
		h.release(nil, kindTemplate, "sql_query", sqlQueryDoc)
		pins := h.mustResolve(t1, "", "")

		h.release(nil, kindTemplate, "answer", answerDocV2) // v2 成为默认，v1 退役
		h.release(nil, kindTemplate, "judge", judgeDoc)     // 新模板

		pinned, err := h.listTemplates(t1, pins, 0)
		if err != nil {
			t.Fatal(err)
		}
		if got := versions(pinned); len(got) != 2 || got["answer"] != "v1" || got["sql_query"] != "v1" {
			t.Errorf("按固定的版本查询不受新发布与晋升影响：%v", got)
		}
		for _, tpl := range pinned {
			if tpl.GetName() == "answer" && tpl.GetState() != stateRetired {
				t.Errorf("固定的 answer@v1 现在是退役状态，照样读得到：%s", tpl.GetState())
			}
		}
		current, err := h.listTemplates(t1, nil, 0)
		if err != nil {
			t.Fatal(err)
		}
		if got := versions(current); got["answer"] != "v2" || got["judge"] != "v1" {
			t.Errorf("不带固定版本时列出当前默认版本：%v", got)
		}
	})
}

func TestPinnedListingFailsClosedOnUnknownVersions(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		h.release(nil, kindTemplate, "answer", answerDoc)
		pins := &workflowv1.PinnedVersions{NodeTemplates: map[string]string{"answer": "v1", "ghost": "v3"}}
		_, err := h.listTemplates(t1, pins, 0)
		wantCode(t, "固定的版本不存在", err, connect.CodeNotFound)
	})
}

func TestResolvePinsTheRecipeOfTheTaskFamily(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		h.release(nil, kindTemplate, "answer", answerDoc)
		h.release(nil, kindRecipe, "refund_analysis", recipeDoc(t, "analytics.attribution", "none", "answer@v1"))
		h.mustPublish(nil, kindRecipe, "naming", recipeDoc(t, "creative.naming", "none", "answer@v1")) // 只有影子
		h.release(nil, kindRecipe, "weekly_a", recipeDoc(t, "research.weekly", "none", "answer@v1"))
		h.release(nil, kindRecipe, "weekly_b", recipeDoc(t, "research.weekly", "none", "answer@v1"))

		cases := []struct {
			name, family, recipe, want string
		}{
			{"按任务族取唯一的默认 Recipe", "analytics.attribution", "", "refund_analysis@v1"},
			{"指定的 Recipe 原样固定", "", "refund_analysis@v1", "refund_analysis@v1"},
			{"任务族只有影子版本时不固定", "creative.naming", "", ""},
			{"任务族有多个默认 Recipe 时不替 Planner 选", "research.weekly", "", ""},
			{"没有这个任务族", "travel.booking", "", ""},
			{"不给任务族也不指定", "", "", ""},
		}
		for _, c := range cases {
			t.Run(c.name, func(t *testing.T) {
				if got := h.mustResolve(t1, c.family, c.recipe).GetRecipe(); got != c.want {
					t.Errorf("固定的 Recipe 应为 %q，实际 %q", c.want, got)
				}
			})
		}
		_, err := h.resolve(t1, "", "refund_analysis@v7")
		wantCode(t, "指定的 Recipe 不存在", err, connect.CodeNotFound)
		_, err = h.resolve(t1, "", "naming@v1")
		wantCode(t, "指定影子版本的 Recipe（影子不外泄）", err, connect.CodeNotFound)
		_, err = h.resolve(t1, "", "refund_analysis")
		wantCode(t, "指定的 Recipe 不是 name@vN", err, connect.CodeInvalidArgument)
	})
}

func TestListingsArePagedInNameOrder(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		for _, name := range []string{"echo", "bravo", "delta", "alpha", "charlie"} {
			h.release(nil, kindTemplate, name, answerDoc)
		}
		listed, err := h.listTemplates(t1, nil, 2)
		if err != nil {
			t.Fatal(err)
		}
		if got := names(listed); !slices.Equal(got, []string{"alpha", "bravo", "charlie", "delta", "echo"}) {
			t.Errorf("分页取完的结果按名字排序、不重不漏：%v", got)
		}
		resp, err := h.client.ListNodeTemplates(context.Background(), connect.NewRequest(&registryv1.ListNodeTemplatesRequest{Tenant: t1, PageSize: 2}))
		if err != nil {
			t.Fatal(err)
		}
		if n := len(resp.Msg.GetNodeTemplates()); n != 2 || resp.Msg.GetNextPageToken() == "" {
			t.Errorf("page_size 2 的第一页有 2 条和下一页的令牌：%d 条，令牌 %q", n, resp.Msg.GetNextPageToken())
		}
		_, err = h.client.ListNodeTemplates(context.Background(), connect.NewRequest(&registryv1.ListNodeTemplatesRequest{Tenant: t1, PageToken: "not-a-token"}))
		wantCode(t, "伪造的分页令牌", err, connect.CodeInvalidArgument)
	})
}

// --- 作用域 ---

func TestPlatformEntriesAreVisibleToEveryTenant(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		h.release(nil, kindTemplate, "answer", answerDoc)
		h.release(nil, kindRecipe, "refund_analysis", recipeDoc(t, "analytics.attribution", "none", "answer@v1"))
		h.release(nil, kindProfile, profileID, profileDoc)
		for _, tenant := range []*commonv1.TenantContext{t1, t2} {
			if _, err := h.getTemplate(tenant, "answer", ""); err != nil {
				t.Errorf("%s 读得到平台的模板：%v", tenant.GetTenantId(), err)
			}
			if _, err := h.getRecipe(tenant, "refund_analysis", ""); err != nil {
				t.Errorf("%s 读得到平台的 Recipe：%v", tenant.GetTenantId(), err)
			}
			resp, err := h.client.GetModelProfile(context.Background(), connect.NewRequest(&registryv1.GetModelProfileRequest{Tenant: tenant, Id: profileID}))
			if err != nil || resp.Msg.GetModelProfile().GetVersion() != "v1" {
				t.Errorf("%s 读得到平台的 Model Profile：%v", tenant.GetTenantId(), err)
			}
		}
	})
}

func TestTenantRecipesAreInvisibleToOtherTenants(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		h.release(nil, kindTemplate, "answer", answerDoc)
		h.release(t1, kindRecipe, "t1_close_books", recipeDoc(t, "finance.close", "none", "answer@v1"))

		if _, err := h.getRecipe(t1, "t1_close_books", ""); err != nil {
			t.Errorf("t1 读得到自己的 Recipe：%v", err)
		}
		if got, err := h.listRecipes(t1, "finance.close"); err != nil || !slices.Equal(recipeRefs(got), []string{"t1_close_books@v1"}) {
			t.Errorf("t1 的列表里有自己的 Recipe：%v（%v）", recipeRefs(got), err)
		}
		_, err := h.getRecipe(t2, "t1_close_books", "")
		wantCode(t, "t2 读 t1 的 Recipe", err, connect.CodeNotFound)
		_, err = h.getRecipe(t2, "t1_close_books", "v1")
		wantCode(t, "t2 按版本号读 t1 的 Recipe", err, connect.CodeNotFound)
		if got, err := h.listRecipes(t2, ""); err != nil || len(got) != 0 {
			t.Errorf("t2 的列表里没有 t1 的 Recipe：%v（%v）", recipeRefs(got), err)
		}
		if got := h.mustResolve(t2, "finance.close", "").GetRecipe(); got != "" {
			t.Errorf("t2 按任务族固定不到 t1 的 Recipe：%q", got)
		}
		_, err = h.promote(t2, kindRecipe, "t1_close_books", "v1", passingEvidence)
		wantCode(t, "t2 晋升 t1 的 Recipe", err, connect.CodeNotFound)
	})
}

func TestTenantRecipeCannotTakeAPlatformName(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		h.release(nil, kindTemplate, "answer", answerDoc)
		h.release(nil, kindRecipe, "refund_analysis", recipeDoc(t, "analytics.attribution", "none", "answer@v1"))
		_, err := h.publish(t1, kindRecipe, "refund_analysis", recipeDoc(t, "analytics.attribution", "none", "answer@v1"))
		wantCode(t, "租户发布与平台同名的 Recipe", err, connect.CodeAborted)
		got, err := h.getRecipe(t1, "refund_analysis", "")
		if err != nil || got.GetVersion() != "v1" {
			t.Errorf("t1 读到的仍是平台的 Recipe：%+v（%v）", got, err)
		}
	})
}

func TestPlatformRecipeWinsOverAnOlderTenantRecipeOfTheSameName(t *testing.T) {
	// 平台发布时看不到各租户的作用域，挡不住与租户已有的 Recipe 同名；读的时候平台的优先，租户不能借同名覆盖平台的做法。
	eachBackend(t, func(t *testing.T, h *harness) {
		h.release(nil, kindTemplate, "answer", answerDoc)
		h.release(nil, kindTemplate, "sql_query", sqlQueryDoc)
		h.release(t1, kindRecipe, "weekly", recipeDoc(t, "research.weekly", "none", "answer@v1"))
		platform := recipeDoc(t, "research.weekly", "read", "sql_query@v1", "answer@v1")
		h.release(nil, kindRecipe, "weekly", platform)

		got, err := h.getRecipe(t1, "weekly", "")
		if err != nil || !proto.Equal(got.GetDocument().GetValue(), value(t, platform)) {
			t.Errorf("同名时 t1 读到平台的 Recipe：%+v（%v）", got, err)
		}
		listed, err := h.listRecipes(t1, "research.weekly")
		if err != nil || len(listed) != 1 {
			t.Errorf("同名的 Recipe 在列表里只出现一次（平台的）：%v（%v）", recipeRefs(listed), err)
		}
	})
}

func TestTenantPoliciesAreIsolated(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		h.mustPublish(t1, kindPolicy, "", policyDoc)
		_, err := h.getPolicy(t2, "")
		wantCode(t, "t2 没有租户策略", err, connect.CodeNotFound)
		_, err = h.getPolicy(t2, "v1")
		wantCode(t, "t2 按版本号读不到 t1 的策略", err, connect.CodeNotFound)
		if got := h.mustResolve(t2, "", "").GetTenantPolicy(); got != "" {
			t.Errorf("t2 固定不到 t1 的策略：%q", got)
		}
	})
}

func TestReadsWithoutTenantAreDenied(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		h.release(nil, kindTemplate, "answer", answerDoc)
		ctx := context.Background()
		calls := map[string]func(tenant *commonv1.TenantContext) error{
			"GetNodeTemplate": func(tc *commonv1.TenantContext) error {
				_, err := h.client.GetNodeTemplate(ctx, connect.NewRequest(&registryv1.GetNodeTemplateRequest{Tenant: tc, Name: "answer"}))
				return err
			},
			"ListNodeTemplates": func(tc *commonv1.TenantContext) error {
				_, err := h.client.ListNodeTemplates(ctx, connect.NewRequest(&registryv1.ListNodeTemplatesRequest{Tenant: tc}))
				return err
			},
			"GetRecipe": func(tc *commonv1.TenantContext) error {
				_, err := h.client.GetRecipe(ctx, connect.NewRequest(&registryv1.GetRecipeRequest{Tenant: tc, Name: "x"}))
				return err
			},
			"ListRecipes": func(tc *commonv1.TenantContext) error {
				_, err := h.client.ListRecipes(ctx, connect.NewRequest(&registryv1.ListRecipesRequest{Tenant: tc}))
				return err
			},
			"GetModelProfile": func(tc *commonv1.TenantContext) error {
				_, err := h.client.GetModelProfile(ctx, connect.NewRequest(&registryv1.GetModelProfileRequest{Tenant: tc, Id: profileID}))
				return err
			},
			"ListModelProfiles": func(tc *commonv1.TenantContext) error {
				_, err := h.client.ListModelProfiles(ctx, connect.NewRequest(&registryv1.ListModelProfilesRequest{Tenant: tc}))
				return err
			},
			"GetTenantPolicy": func(tc *commonv1.TenantContext) error {
				_, err := h.client.GetTenantPolicy(ctx, connect.NewRequest(&registryv1.GetTenantPolicyRequest{Tenant: tc}))
				return err
			},
			"ResolvePinnedVersions": func(tc *commonv1.TenantContext) error {
				_, err := h.client.ResolvePinnedVersions(ctx, connect.NewRequest(&registryv1.ResolvePinnedVersionsRequest{Tenant: tc}))
				return err
			},
		}
		for name, call := range calls {
			t.Run(name, func(t *testing.T) {
				wantCode(t, name+" 不带租户", call(nil), connect.CodePermissionDenied)
				wantCode(t, name+" 带空的租户 ID", call(&commonv1.TenantContext{}), connect.CodePermissionDenied)
				wantCode(t, name+" 租户 ID 格式不对", call(&commonv1.TenantContext{TenantId: "T 1"}), connect.CodeInvalidArgument)
			})
		}
	})
}

func TestPublishScopeFollowsTheKind(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		cases := []struct {
			name   string
			tenant *commonv1.TenantContext
			kind   registryv1.RegistryKind
			doc    string
		}{
			{"Node Template 只在平台作用域", t1, kindTemplate, answerDoc},
			{"Model Profile 只在平台作用域", t1, kindProfile, profileDoc},
			{"租户策略要有租户", nil, kindPolicy, policyDoc},
			{"Reasoning Template 第一期不收（还没有 schema）", nil, kindReasoning, `{"steps":["列出变量"]}`},
			{"种类未指定", nil, registryv1.RegistryKind_REGISTRY_KIND_UNSPECIFIED, answerDoc},
		}
		for _, c := range cases {
			t.Run(c.name, func(t *testing.T) {
				name := "answer"
				switch c.kind {
				case kindProfile:
					name = profileID
				case kindPolicy:
					name = "" // 一个租户只有一份策略，不起名字
				}
				_, err := h.publish(c.tenant, c.kind, name, c.doc)
				wantCode(t, c.name, err, connect.CodeInvalidArgument)
			})
		}
	})
}

func TestErrorsCarryTheErrorTrailer(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		_, err := h.getTemplate(t1, "nope", "")
		code, detail := errorDetail(t, err)
		if code != connect.CodeNotFound || detail.GetCategory() != commonv1.ErrorCategory_ERROR_CATEGORY_NOT_FOUND || detail.GetCode() == "" {
			t.Errorf("NotFound 的 trailer：%v %+v", code, detail)
		}
		_, err = h.publish(nil, kindTemplate, "answer", `{"executor":"model"}`)
		code, detail = errorDetail(t, err)
		if code != connect.CodeInvalidArgument || detail.GetCategory() != commonv1.ErrorCategory_ERROR_CATEGORY_INVALID {
			t.Errorf("Invalid 的 trailer：%v %+v", code, detail)
		}
	})
}

// --- 业务库 ---

func TestMigrationsDoNotCollideWithTheTaskStore(t *testing.T) {
	// 同一个 RelationalStore 按 Version 记迁移：Registry 与 taskstore 的版本号重了，后登记的会顶掉先登记的。
	seen := map[int]string{}
	for _, m := range taskstore.Migrations() {
		seen[m.Version] = m.Name
	}
	for _, m := range registry.Migrations() {
		if other, ok := seen[m.Version]; ok {
			t.Errorf("Registry 的迁移 %s 与 taskstore 的 %s 版本号都是 %d", m.Name, other, m.Version)
		}
	}
	if len(registry.Migrations()) == 0 {
		t.Error("Registry 要有自己的表")
	}
}

func TestRegistryTablesAreCoveredByRowLevelSecurity(t *testing.T) {
	p := backends[1].open(t).(*postgres.Provider)
	if err := p.Migrate(context.Background(), registry.Migrations()); err != nil {
		t.Fatal(err)
	}
	audits, err := p.AuditRLS(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(audits) == 0 {
		t.Fatal("应当核对 Registry 的表")
	}
	for _, a := range audits {
		if !a.Covered() {
			t.Errorf("表 %s 的租户隔离不完整：%+v", a.Table, a)
		}
	}
}
