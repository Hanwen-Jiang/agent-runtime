package registry_test

// N-14 验收（契约 c1836ba，docs/plan/n14-skill-contract.md 的“验收”）：Skill 的三层、同名上层优先、版本发布即生效且不可变、
// 固定的版本照旧读得到、别人的用户层读不到、目录不带正文、内容不变不出新版本、收回、每人 100 个、正文不进日志。
// 平台层与租户层经 Registry 的 Publish（要发布令牌），用户层经 Registry 的导出方法（SkillService 调它们）。

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"

	"connectrpc.com/connect"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	registryv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/registry/v1"
	workflowv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workflow/v1"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/registry"
)

const kindSkill = registryv1.RegistryKind_REGISTRY_KIND_SKILL

var (
	alice = &commonv1.TenantContext{TenantId: "t1", UserId: "alice"}
	bob   = &commonv1.TenantContext{TenantId: "t1", UserId: "bob"}
	eve   = &commonv1.TenantContext{TenantId: "t2", UserId: "eve"}
)

func skillDoc(description, instructions string) string {
	return fmt.Sprintf(`{"description":%q,"instructions":%q}`, description, instructions)
}

func (h *harness) getSkill(tenant *commonv1.TenantContext, name, version string) (*registryv1.Skill, error) {
	resp, err := h.client.GetSkill(context.Background(), connect.NewRequest(&registryv1.GetSkillRequest{Tenant: tenant, Name: name, Version: version}))
	if err != nil {
		return nil, err
	}
	return resp.Msg.GetSkill(), nil
}

func (h *harness) listSkills(tenant *commonv1.TenantContext, pins map[string]string) ([]*registryv1.Skill, error) {
	var out []*registryv1.Skill
	token := ""
	for range 100 {
		req := &registryv1.ListSkillsRequest{Tenant: tenant, PageSize: 2, PageToken: token}
		if pins != nil {
			req.PinnedVersions = &workflowv1.PinnedVersions{Skills: pins}
		}
		resp, err := h.client.ListSkills(context.Background(), connect.NewRequest(req))
		if err != nil {
			return nil, err
		}
		out = append(out, resp.Msg.GetSkills()...)
		if token = resp.Msg.GetNextPageToken(); token == "" {
			return out, nil
		}
	}
	h.t.Fatal("分页没有结束")
	return nil, nil
}

func (h *harness) putUser(tenant *commonv1.TenantContext, name, doc string) (registry.UserSkillResult, error) {
	return h.reg.PutUserSkill(context.Background(), tenant, name, payload(h.t, doc))
}

func (h *harness) mustPutUser(tenant *commonv1.TenantContext, name, doc string) registry.UserSkillResult {
	h.t.Helper()
	got, err := h.putUser(tenant, name, doc)
	if err != nil {
		h.t.Fatalf("PutUserSkill %s：%v", name, err)
	}
	return got
}

func instructionsOf(t *testing.T, s *registryv1.Skill) string {
	t.Helper()
	fields := s.GetDocument().GetValue().GetStructValue().GetFields()
	if fields == nil {
		t.Fatalf("%s %s 没有正文", s.GetName(), s.GetVersion())
	}
	return fields["instructions"].GetStringValue()
}

func providerCode(err error) (providererr.Category, string) {
	var pe *providererr.Error
	if errors.As(err, &pe) {
		return pe.Category, pe.Code
	}
	return 0, ""
}

func skillNames(skills []*registryv1.Skill) []string {
	var out []string
	for _, s := range skills {
		out = append(out, s.GetName()+"="+s.GetVersion())
	}
	return out
}

func TestPinnedSkillVersionsStayReadableAfterANewVersion(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		if v := h.mustPublish(nil, kindSkill, "weekly_report", skillDoc("写周报", "第一版")); v != "platform/v1" {
			t.Fatalf("平台层第一版应是 platform/v1：%s", v)
		}
		pins := h.mustResolve(alice, "", "").GetSkills()
		if pins["weekly_report"] != "platform/v1" {
			t.Fatalf("新 Task 固定看得到的当前 Skill：%v", pins)
		}
		if v := h.mustPublish(nil, kindSkill, "weekly_report", skillDoc("写周报", "第二版")); v != "platform/v2" {
			t.Fatalf("第二版：%s", v)
		}
		old, err := h.getSkill(alice, "weekly_report", "platform/v1")
		if err != nil || instructionsOf(t, old) != "第一版" || old.GetState() != stateRetired {
			t.Fatalf("固定 v1 的 Task 仍读 v1（已退役）：%v %v", old, err)
		}
		listed, err := h.listSkills(alice, pins)
		if err != nil || len(listed) != 1 || listed[0].GetVersion() != "platform/v1" {
			t.Fatalf("按固定的版本列目录：%v %v", skillNames(listed), err)
		}
		if got := h.mustResolve(alice, "", "").GetSkills()["weekly_report"]; got != "platform/v2" {
			t.Errorf("新 Task 固定 v2：%s", got)
		}
		current, err := h.getSkill(alice, "weekly_report", "")
		if err != nil || instructionsOf(t, current) != "第二版" || current.GetScope() != registryv1.SkillScope_SKILL_SCOPE_PLATFORM {
			t.Errorf("不写版本取当前版本：%v %v", current, err)
		}
	})
}

func TestUnchangedSkillContentDoesNotMakeANewVersion(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		doc := skillDoc("写周报", "按模块归类")
		first := h.mustPutUser(alice, "my_report", doc)
		if !first.Created || !first.NewVersion || first.Skill.GetVersion() != "user/v1" || first.Skill.GetDigest() == "" {
			t.Fatalf("第一次：%+v", first)
		}
		again := h.mustPutUser(alice, "my_report", `{"instructions":"按模块归类","description":"写周报"}`)
		if again.Created || again.NewVersion || again.Skill.GetVersion() != "user/v1" || again.Skill.GetDigest() != first.Skill.GetDigest() {
			t.Fatalf("内容相同（键的顺序不同）不出新版本：%+v", again)
		}
		changed := h.mustPutUser(alice, "my_report", skillDoc("写周报", "按模块归类，每类一句"))
		if changed.Created || !changed.NewVersion || changed.Skill.GetVersion() != "user/v2" || changed.Skill.GetDigest() == first.Skill.GetDigest() {
			t.Fatalf("改一个字出新版本：%+v", changed)
		}
		if v := h.mustPublish(nil, kindSkill, "shared", skillDoc("共用", "x")); v != "platform/v1" {
			t.Fatal(v)
		}
		if v := h.mustPublish(nil, kindSkill, "shared", skillDoc("共用", "x")); v != "platform/v1" {
			t.Errorf("平台层内容不变也不出新版本：%s", v)
		}
	})
}

func TestLowerLayersCannotShadowUpperLayers(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		h.mustPublish(nil, kindSkill, "deploy_guide", skillDoc("平台的", "平台"))
		_, err := h.publish(alice, kindSkill, "deploy_guide", skillDoc("租户的", "租户"))
		if _, detail := errorDetail(t, err); detail.GetCode() != "name_taken" || detail.GetCategory() != providererr.Conflict {
			t.Errorf("租户层不能发布平台已有的名字：%v / %q", detail.GetCategory(), detail.GetCode())
		}
		if _, err := h.putUser(alice, "deploy_guide", skillDoc("我的", "我")); err == nil {
			t.Error("用户层不能发布平台已有的名字")
		} else if cat, code := providerCode(err); cat != providererr.Conflict || code != "name_taken" {
			t.Errorf("应是 Conflict / name_taken：%v %q", cat, code)
		}
		h.mustPublish(alice, kindSkill, "team_style", skillDoc("租户的", "租户"))
		if _, err := h.putUser(alice, "team_style", skillDoc("我的", "我")); err == nil {
			t.Error("用户层不能发布本租户已有的名字")
		}

		// 上层之后才有的同名条目遮住下层的：新 Task 固定上层的，已固定下层的照旧读下层。
		h.mustPutUser(alice, "lint_rules", skillDoc("我的", "我的规则"))
		before := h.mustResolve(alice, "", "").GetSkills()
		if before["lint_rules"] != "user/v1" {
			t.Fatalf("用户层固定成 user/v1：%v", before)
		}
		h.mustPublish(nil, kindSkill, "lint_rules", skillDoc("平台的", "平台的规则"))
		if got := h.mustResolve(alice, "", "").GetSkills()["lint_rules"]; got != "platform/v1" {
			t.Errorf("新 Task 固定平台的：%s", got)
		}
		mine, err := h.getSkill(alice, "lint_rules", "user/v1")
		if err != nil || instructionsOf(t, mine) != "我的规则" || !mine.GetShadowed() {
			t.Errorf("已固定用户层的 Task 照旧读用户层（标着被遮住）：%v %v", mine, err)
		}
		own, err := h.reg.OwnerSkills(context.Background(), alice, 0, "")
		if err != nil {
			t.Fatal(err)
		}
		var shadowed bool
		for _, s := range own.Skills {
			if s.GetName() == "lint_rules" && s.GetScope() == registryv1.SkillScope_SKILL_SCOPE_USER {
				shadowed = s.GetShadowed()
			}
		}
		if !shadowed {
			t.Errorf("登记者自己列出时看得到被遮住的：%v", skillNames(own.Skills))
		}
		visible, _ := h.listSkills(alice, nil)
		for _, s := range visible {
			if s.GetScope() == registryv1.SkillScope_SKILL_SCOPE_USER && s.GetName() == "lint_rules" {
				t.Errorf("被遮住的不进 Planner 的目录：%v", skillNames(visible))
			}
		}
	})
}

func TestOthersCannotReadAUsersSkill(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		h.mustPutUser(alice, "secret_sauce", skillDoc("我的做法", "只给我"))
		for _, who := range []*commonv1.TenantContext{bob, eve} {
			_, err := h.getSkill(who, "secret_sauce", "")
			if code, detail := errorDetail(t, err); code != connect.CodeNotFound || detail.GetCode() != "skill_not_found" {
				t.Errorf("%s 读不到 alice 的：%v / %q", who.GetUserId(), code, detail.GetCode())
			}
			_, err = h.getSkill(who, "secret_sauce", "user/v1")
			if code, _ := errorDetail(t, err); code != connect.CodeNotFound {
				t.Errorf("%s 按版本也读不到：%v", who.GetUserId(), code)
			}
			listed, _ := h.listSkills(who, nil)
			if len(listed) != 0 {
				t.Errorf("%s 的目录里没有 alice 的：%v", who.GetUserId(), skillNames(listed))
			}
			if pins := h.mustResolve(who, "", "").GetSkills(); len(pins) != 0 {
				t.Errorf("%s 的 Task 不固定 alice 的：%v", who.GetUserId(), pins)
			}
		}
		if _, err := h.getSkill(&commonv1.TenantContext{}, "secret_sauce", ""); connect.CodeOf(err) != connect.CodePermissionDenied {
			t.Errorf("缺租户 denied：%v", err)
		}
		listed, err := h.listSkills(alice, nil)
		if err != nil || len(listed) != 1 || listed[0].GetDocument() != nil || listed[0].GetDescription() != "我的做法" || listed[0].GetOwner() != "alice" {
			t.Errorf("目录只有名字、版本与说明，不带正文：%v %v", listed, err)
		}
	})
}

func TestSkillDocumentsAreChecked(t *testing.T) {
	big := strings.Repeat("长", 30000)
	cases := map[string]struct{ name, doc, code string }{
		"大写的名字":       {"Weekly", skillDoc("x", "y"), "invalid_name"},
		"太长的名字":       {strings.Repeat("a", 65), skillDoc("x", "y"), "invalid_name"},
		"声明 effect":   {"ok_name", `{"description":"x","instructions":"y","effect":"write_irreversible"}`, "invalid_document"},
		"路径不分大小写重复":   {"ok_name", `{"description":"x","instructions":"y","files":[{"path":"A.md","content":"1"},{"path":"a.md","content":"2"}]}`, "duplicate_path"},
		"一个路径是另一个的目录": {"ok_name", `{"description":"x","instructions":"y","files":[{"path":"docs","content":"1"},{"path":"docs/a.md","content":"2"}]}`, "duplicate_path"},
		"超过 256 KiB":  {"ok_name", fmt.Sprintf(`{"description":"x","instructions":%q,"files":[{"path":"a.md","content":%q},{"path":"b.md","content":%q}]}`, big, big, big), "skill_too_large"},
	}
	eachBackend(t, func(t *testing.T, h *harness) {
		for label, c := range cases {
			_, err := h.putUser(alice, c.name, c.doc)
			if cat, code := providerCode(err); cat != providererr.Invalid || code != c.code {
				t.Errorf("%s：应是 Invalid / %s，实际 %v / %q（%v）", label, c.code, cat, code, err)
			}
			_, err = h.publish(nil, kindSkill, c.name, c.doc)
			if code, detail := errorDetail(t, err); code != connect.CodeInvalidArgument || detail.GetCode() != c.code {
				t.Errorf("%s（平台层）：%v / %q", label, code, detail.GetCode())
			}
		}
		if _, err := h.putUser(&commonv1.TenantContext{TenantId: "t1"}, "ok_name", skillDoc("x", "y")); err == nil {
			t.Error("用户层要有用户")
		}
	})
}

func TestRetiredSkillsLeaveTheCatalogButPinsStillRead(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		h.mustPutUser(alice, "old_way", skillDoc("旧做法", "旧"))
		pins := h.mustResolve(alice, "", "").GetSkills()
		version, retired, err := h.reg.RetireUserSkill(context.Background(), alice, "old_way")
		if err != nil || !retired || version != "user/v1" {
			t.Fatalf("收回：%s %v %v", version, retired, err)
		}
		if _, retired, err := h.reg.RetireUserSkill(context.Background(), alice, "old_way"); err != nil || retired {
			t.Errorf("再收回是幂等的：%v %v", retired, err)
		}
		if _, _, err := h.reg.RetireUserSkill(context.Background(), bob, "old_way"); err == nil {
			t.Error("不是本人的收回不了")
		}
		if got := h.mustResolve(alice, "", "").GetSkills(); len(got) != 0 {
			t.Errorf("收回的不再被新 Task 固定：%v", got)
		}
		if listed, _ := h.listSkills(alice, nil); len(listed) != 0 {
			t.Errorf("收回的不进目录：%v", skillNames(listed))
		}
		if listed, err := h.listSkills(alice, pins); err != nil || len(listed) != 1 {
			t.Errorf("已固定它的 Task 照旧列出：%v %v", skillNames(listed), err)
		}
		if _, err := h.getSkill(alice, "old_way", pins["old_way"]); err != nil {
			t.Errorf("已固定它的 Task 照旧读：%v", err)
		}
		again := h.mustPutUser(alice, "old_way", skillDoc("旧做法", "旧"))
		if !again.NewVersion || again.Skill.GetVersion() != "user/v2" {
			t.Errorf("再发布接着编号：%+v", again)
		}
	})
}

func TestEachUserHasAtMostAHundredSkills(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		for i := range registry.MaxSkillsPerUser {
			h.mustPutUser(alice, fmt.Sprintf("s%03d", i), skillDoc("x", fmt.Sprint(i)))
		}
		if _, err := h.putUser(alice, "one_more", skillDoc("x", "y")); !errors.Is(err, registry.ErrSkillLimit) {
			t.Fatalf("第 101 个：%v", err)
		}
		h.mustPutUser(alice, "s000", skillDoc("x", "新内容")) // 已有的出新版本不算新的一个
		h.mustPutUser(bob, "one_more", skillDoc("x", "y")) // 别人不受影响
		if _, _, err := h.reg.RetireUserSkill(context.Background(), alice, "s001"); err != nil {
			t.Fatal(err)
		}
		h.mustPutUser(alice, "one_more", skillDoc("x", "y"))
	})
}

func TestSkillBodiesStayOutOfTheLogs(t *testing.T) {
	const marker = "skill-body-marker-9f3a1c"
	eachBackend(t, func(t *testing.T, h *harness) {
		h.mustPublish(nil, kindSkill, "logged", skillDoc("x", marker))
		h.mustPutUser(alice, "mine", fmt.Sprintf(`{"description":"x","instructions":"y","files":[{"path":"a.md","content":%q}]}`, marker))
		if _, err := h.getSkill(alice, "mine", ""); err != nil {
			t.Fatal(err)
		}
		if strings.Contains(h.logs.String(), marker) {
			t.Error("日志里不能有正文")
		}
	})
}

// 列表与 Get 返回的正文是 JsonPayload（schema_id 是 skill.schema.json 的 $id）。
func TestSkillPayloadCarriesTheSchemaID(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		h.mustPublish(nil, kindSkill, "with_files", `{"description":"x","instructions":"y","files":[{"path":"a.md","content":"z"}]}`)
		got, err := h.getSkill(alice, "with_files", "")
		if err != nil {
			t.Fatal(err)
		}
		if !strings.HasSuffix(got.GetDocument().GetSchemaId(), "skill.schema.json") || got.GetPublishedAt() == nil {
			t.Errorf("schema_id 与发布时间：%v", got)
		}
		files := got.GetDocument().GetValue().GetStructValue().GetFields()["files"].GetListValue().GetValues()
		if len(files) != 1 || files[0].GetStructValue().GetFields()["path"].GetStringValue() != "a.md" {
			t.Errorf("文件原样保存：%v", files)
		}
	})
}
