package registry_test

// N-14 的契约草案（等人审）落地之前：GetSkill、ListSkills 是 Unimplemented，Publish 不收 SKILL，新 Task 不固定 Skill。

import (
	"context"
	"testing"

	"connectrpc.com/connect"

	registryv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/registry/v1"
)

func TestSkillsAreUnavailableUntilTheContractIsApproved(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		ctx := context.Background()
		if _, err := h.client.GetSkill(ctx, connect.NewRequest(&registryv1.GetSkillRequest{Tenant: t1, Name: "weekly_report"})); connect.CodeOf(err) != connect.CodeUnimplemented {
			t.Errorf("GetSkill 应是 Unimplemented：%v", err)
		}
		if _, err := h.client.ListSkills(ctx, connect.NewRequest(&registryv1.ListSkillsRequest{Tenant: t1})); connect.CodeOf(err) != connect.CodeUnimplemented {
			t.Errorf("ListSkills 应是 Unimplemented：%v", err)
		}
		_, err := h.publish(t1, registryv1.RegistryKind_REGISTRY_KIND_SKILL, "weekly_report", `{"description":"写周报","instructions":"按模块归类"}`)
		if code, detail := errorDetail(t, err); code != connect.CodeInvalidArgument || detail.GetCode() != "unsupported_kind" {
			t.Errorf("Publish 还不收 SKILL：%v / %q", code, detail.GetCode())
		}
		if pins := h.mustResolve(t1, "", ""); len(pins.GetSkills()) != 0 {
			t.Errorf("新 Task 还不固定 Skill：%v", pins.GetSkills())
		}
	})
}
