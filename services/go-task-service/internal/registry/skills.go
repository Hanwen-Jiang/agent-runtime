package registry

// Skill（N-14，docs/plan/n14-skill-contract.md）：契约草案等人审（S-01），还没实现。在实现之前 GetSkill、ListSkills
// 一律 Unimplemented，Publish 照旧不收 SKILL（unsupported_kind），ResolvePinnedVersions 不固定 Skill。

import (
	"context"
	"errors"

	"connectrpc.com/connect"

	registryv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/registry/v1"
)

var errSkillsUnavailable = errors.New("Skill（N-14）还没实现：契约草案等人审")

func (r *Registry) GetSkill(context.Context, *connect.Request[registryv1.GetSkillRequest]) (*connect.Response[registryv1.GetSkillResponse], error) {
	return nil, connect.NewError(connect.CodeUnimplemented, errSkillsUnavailable)
}

func (r *Registry) ListSkills(context.Context, *connect.Request[registryv1.ListSkillsRequest]) (*connect.Response[registryv1.ListSkillsResponse], error) {
	return nil, connect.NewError(connect.CodeUnimplemented, errSkillsUnavailable)
}
