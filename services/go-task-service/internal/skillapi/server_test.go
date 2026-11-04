package skillapi

// N-14 的 SkillService（契约 proto/agentruntime/skill/v1/skill.proto，c1836ba，2026-11-04 人审通过）的验收：
//
//  1. 发布、列出、读、收回本人的用户层 Skill；内容不变不出新版本；目录不带正文，读才带。
//  2. 用户来自认证身份：别的用户读不到、收回不了（与不存在一样，NotFound skill_not_found）；请求里的 tenant_id 只校验一致；
//     没认证 Unauthenticated。错误的 code 在 google.rpc.ErrorInfo 的 reason 里（同 Task API）。
//  3. 平台层、租户层已有的名字：name_taken；第 101 个：ResourceExhausted（skill_limit）。
//  4. 没接 Registry 时四个方法都是 Unimplemented（skills_unavailable）。
//  5. 正文不进日志（审计只记租户、用户、名字、版本、动作）。
// Registry 的三层规则在 internal/registry 的测试里（两种后端）；这里对着 SQLite 上真的 Registry。

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"connectrpc.com/connect"
	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/protobuf/types/known/structpb"

	"github.com/agentruntime/agent-runtime/libs/go-core/dev/localingress"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/sqlitestore"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	skillv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/skill/v1"
	"github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/skill/v1/skillv1connect"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/registry"
)

var ctx = context.Background()

const marker = "skill-body-marker-77aa01"

type harness struct {
	ing    *localingress.Provider
	reg    *registry.Registry
	logs   *bytes.Buffer
	client skillv1connect.SkillServiceClient
}

func newHarness(t *testing.T, withRegistry bool) *harness {
	t.Helper()
	store, err := sqlitestore.New(sqlitestore.Options{Dir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = store.Close() })
	if err := store.Migrate(ctx, registry.Migrations()); err != nil {
		t.Fatal(err)
	}
	h := &harness{ing: localingress.New(localingress.Options{SigningKey: []byte(strings.Repeat("k", 32)), Limit: 10000, Window: time.Minute}), logs: &bytes.Buffer{}}
	logger := slog.New(slog.NewTextHandler(h.logs, nil))
	deps := Deps{Ingress: h.ing, Logger: logger}
	if withRegistry {
		h.reg, err = registry.New(registry.Options{Store: store, SchemaDir: filepath.Join("..", "..", "..", "..", "schemas"), Logger: logger})
		if err != nil {
			t.Fatal(err)
		}
		deps.Skills = h.reg
	}
	srv, err := New(deps)
	if err != nil {
		t.Fatal(err)
	}
	mux := http.NewServeMux()
	srv.Mount(mux)
	ts := httptest.NewServer(mux)
	t.Cleanup(ts.Close)
	h.client = skillv1connect.NewSkillServiceClient(ts.Client(), ts.URL)
	return h
}

func as[T any](h *harness, tenant, user string, msg *T) *connect.Request[T] {
	r := connect.NewRequest(msg)
	if user != "" {
		r.Header().Set("Authorization", "Bearer "+h.ing.IssueToken(tenant, user, time.Hour))
	}
	return r
}

func doc(t *testing.T, description, instructions string) *commonv1.JsonPayload {
	t.Helper()
	var v structpb.Value
	raw, _ := json.Marshal(map[string]string{"description": description, "instructions": instructions})
	if err := v.UnmarshalJSON(raw); err != nil {
		t.Fatal(err)
	}
	return &commonv1.JsonPayload{Value: &v}
}

func reasonOf(t *testing.T, err error) (connect.Code, string) {
	t.Helper()
	var ce *connect.Error
	if !errors.As(err, &ce) {
		t.Fatalf("应是 connect 的错误：%v", err)
	}
	for _, d := range ce.Details() {
		if v, err := d.Value(); err == nil {
			if info, ok := v.(*errdetails.ErrorInfo); ok {
				return ce.Code(), info.GetReason()
			}
		}
	}
	return ce.Code(), ""
}

func (h *harness) put(t *testing.T, tenant, user, name, description, instructions string) (*skillv1.PutSkillResponse, error) {
	t.Helper()
	resp, err := h.client.PutSkill(ctx, as(h, tenant, user, &skillv1.PutSkillRequest{TenantId: tenant, Name: name, Document: doc(t, description, instructions)}))
	if err != nil {
		return nil, err
	}
	return resp.Msg, nil
}

func TestPutListGetRetireOwnSkills(t *testing.T) {
	h := newHarness(t, true)
	first, err := h.put(t, "t1", "alice", "weekly_report", "写周报", marker)
	if err != nil || !first.GetCreated() || !first.GetNewVersion() || first.GetSkill().GetVersion() != "user/v1" || first.GetSkill().GetOwner() != "alice" {
		t.Fatalf("发布：%v %v", first, err)
	}
	same, err := h.put(t, "t1", "alice", "weekly_report", "写周报", marker)
	if err != nil || same.GetCreated() || same.GetNewVersion() || same.GetSkill().GetVersion() != "user/v1" {
		t.Fatalf("内容不变不出新版本：%v %v", same, err)
	}
	if next, err := h.put(t, "t1", "alice", "weekly_report", "写周报", marker+"，改了"); err != nil || !next.GetNewVersion() || next.GetSkill().GetVersion() != "user/v2" {
		t.Fatalf("改了出新版本：%v %v", next, err)
	}
	list, err := h.client.ListSkills(ctx, as(h, "t1", "alice", &skillv1.ListSkillsRequest{TenantId: "t1"}))
	if err != nil || len(list.Msg.GetSkills()) != 1 || list.Msg.GetSkills()[0].GetDocument() != nil || list.Msg.GetSkills()[0].GetDescription() != "写周报" {
		t.Fatalf("列出：不带正文：%v %v", list, err)
	}
	got, err := h.client.GetSkill(ctx, as(h, "t1", "alice", &skillv1.GetSkillRequest{TenantId: "t1", Name: "weekly_report", Version: "user/v1"}))
	if err != nil || got.Msg.GetSkill().GetDocument().GetValue().GetStructValue().GetFields()["instructions"].GetStringValue() != marker {
		t.Fatalf("按版本读旧的：%v %v", got, err)
	}
	retired, err := h.client.RetireSkill(ctx, as(h, "t1", "alice", &skillv1.RetireSkillRequest{TenantId: "t1", Name: "weekly_report"}))
	if err != nil || !retired.Msg.GetRetired() || retired.Msg.GetVersion() != "user/v2" {
		t.Fatalf("收回：%v %v", retired, err)
	}
	again, err := h.client.RetireSkill(ctx, as(h, "t1", "alice", &skillv1.RetireSkillRequest{TenantId: "t1", Name: "weekly_report"}))
	if err != nil || again.Msg.GetRetired() {
		t.Fatalf("再收回幂等：%v %v", again, err)
	}
	if list, _ := h.client.ListSkills(ctx, as(h, "t1", "alice", &skillv1.ListSkillsRequest{TenantId: "t1"})); len(list.Msg.GetSkills()) != 0 {
		t.Errorf("收回后不列出：%v", list.Msg.GetSkills())
	}
	if strings.Contains(h.logs.String(), marker) {
		t.Error("日志里不能有正文")
	}
	if !strings.Contains(h.logs.String(), "weekly_report") {
		t.Error("审计记名字")
	}
}

func TestOnlyTheOwnerSeesAUserSkill(t *testing.T) {
	h := newHarness(t, true)
	if _, err := h.put(t, "t1", "alice", "mine", "我的", "只给我"); err != nil {
		t.Fatal(err)
	}
	_, err := h.client.GetSkill(ctx, as(h, "t1", "bob", &skillv1.GetSkillRequest{TenantId: "t1", Name: "mine"}))
	if code, reason := reasonOf(t, err); code != connect.CodeNotFound || reason != "skill_not_found" {
		t.Errorf("同租户别人读不到：%v %q", code, reason)
	}
	_, err = h.client.RetireSkill(ctx, as(h, "t1", "bob", &skillv1.RetireSkillRequest{TenantId: "t1", Name: "mine"}))
	if code, _ := reasonOf(t, err); code != connect.CodeNotFound {
		t.Errorf("别人收回不了：%v", code)
	}
	if list, _ := h.client.ListSkills(ctx, as(h, "t1", "bob", &skillv1.ListSkillsRequest{TenantId: "t1"})); len(list.Msg.GetSkills()) != 0 {
		t.Errorf("别人的目录里没有：%v", list.Msg.GetSkills())
	}
	_, err = h.client.GetSkill(ctx, as(h, "t2", "alice", &skillv1.GetSkillRequest{TenantId: "t2", Name: "mine"}))
	if code, _ := reasonOf(t, err); code != connect.CodeNotFound {
		t.Errorf("别的租户的同名用户读不到：%v", code)
	}
	_, err = h.client.GetSkill(ctx, as(h, "t1", "alice", &skillv1.GetSkillRequest{TenantId: "t2", Name: "mine"}))
	if code, _ := reasonOf(t, err); code != connect.CodePermissionDenied {
		t.Errorf("tenant_id 与认证身份不一致：%v", code)
	}
	_, err = h.client.ListSkills(ctx, as(h, "t1", "", &skillv1.ListSkillsRequest{TenantId: "t1"}))
	if code, _ := reasonOf(t, err); code != connect.CodeUnauthenticated {
		t.Errorf("没认证：%v", code)
	}
}

func TestNameTakenAndLimit(t *testing.T) {
	h := newHarness(t, true)
	if _, err := h.reg.Seed(ctx, []registry.SeedEntry{{Kind: "skill", Name: "deploy_guide", Document: json.RawMessage(`{"description":"平台的","instructions":"x"}`)}}); err != nil {
		t.Fatal(err)
	}
	_, err := h.put(t, "t1", "alice", "deploy_guide", "我的", "y")
	if _, reason := reasonOf(t, err); reason != "name_taken" {
		t.Errorf("平台已有的名字：%q（%v）", reason, err)
	}
	if list, _ := h.client.ListSkills(ctx, as(h, "t1", "alice", &skillv1.ListSkillsRequest{TenantId: "t1"})); len(list.Msg.GetSkills()) != 1 || list.Msg.GetSkills()[0].GetScope().String() != "SKILL_SCOPE_PLATFORM" {
		t.Errorf("平台层的也列出：%v", list.Msg.GetSkills())
	}
	for i := range registry.MaxSkillsPerUser {
		if _, err := h.put(t, "t1", "alice", fmt.Sprintf("s%03d", i), "x", "y"); err != nil {
			t.Fatal(err)
		}
	}
	_, err = h.put(t, "t1", "alice", "one_more", "x", "y")
	if code, reason := reasonOf(t, err); code != connect.CodeResourceExhausted || reason != "skill_limit" {
		t.Errorf("第 101 个：%v %q", code, reason)
	}
	_, err = h.put(t, "t1", "alice", "Bad Name", "x", "y")
	if code, reason := reasonOf(t, err); code != connect.CodeInvalidArgument || reason != "invalid_name" {
		t.Errorf("名字不合规：%v %q", code, reason)
	}
}

func TestWithoutARegistryTheServiceIsUnavailable(t *testing.T) {
	h := newHarness(t, false)
	_, put := h.put(t, "t1", "alice", "x", "x", "y")
	_, list := h.client.ListSkills(ctx, as(h, "t1", "alice", &skillv1.ListSkillsRequest{TenantId: "t1"}))
	_, get := h.client.GetSkill(ctx, as(h, "t1", "alice", &skillv1.GetSkillRequest{TenantId: "t1", Name: "x"}))
	_, retire := h.client.RetireSkill(ctx, as(h, "t1", "alice", &skillv1.RetireSkillRequest{TenantId: "t1", Name: "x"}))
	for name, err := range map[string]error{"PutSkill": put, "ListSkills": list, "GetSkill": get, "RetireSkill": retire} {
		if code, reason := reasonOf(t, err); code != connect.CodeUnimplemented || reason != "skills_unavailable" {
			t.Errorf("%s：%v %q", name, code, reason)
		}
	}
}
