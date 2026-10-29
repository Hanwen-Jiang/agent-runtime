package registry_test

// TMP 计划（docs/plan/eval-observability-and-runtime-fixes.md）第 6 节：Registry 的任务族词表（种类 TASK_FAMILIES，
// /schemas/task_families.schema.json）。与租户策略一样：发布即生效，不走 Promote；平台默认的一份不带租户、名为 default，
// 租户自己的不带名字、优先。id 不能重复（JSON Schema 表达不了，发布时核对）。

import (
	"context"
	"testing"

	"connectrpc.com/connect"
	"google.golang.org/protobuf/proto"

	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	registryv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/registry/v1"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/registry"
)

const (
	kindFamilies = registryv1.RegistryKind_REGISTRY_KIND_TASK_FAMILIES

	familiesDoc   = `{"families":[{"id":"engineering.ci_fix","description":"修复失败的 CI"},{"id":"engineering.code_explain","description":"讲清仓库结构"}]}`
	familiesDocV2 = `{"families":[{"id":"engineering.ci_fix","description":"修复失败的 CI、测试或构建"}]}`
	ownFamilies   = `{"families":[{"id":"finance.reconcile","description":"对账"}]}`
)

func (h *harness) getFamilies(tenant *commonv1.TenantContext) (*registryv1.TaskFamilies, error) {
	resp, err := h.client.GetTaskFamilies(context.Background(), connect.NewRequest(&registryv1.GetTaskFamiliesRequest{Tenant: tenant}))
	if err != nil {
		return nil, err
	}
	return resp.Msg.GetTaskFamilies(), nil
}

func TestTaskFamiliesFallBackToThePlatformDefault(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		_, err := h.getFamilies(t1)
		wantCode(t, "还没有任何词表", err, connect.CodeNotFound)

		resp, err := h.publish(nil, kindFamilies, registry.DefaultTenantPolicy, familiesDoc)
		if err != nil || resp.GetState() != stateDefault {
			t.Fatalf("平台默认的词表发布即生效：%+v（%v）", resp, err)
		}
		got, err := h.getFamilies(t1)
		if err != nil || !got.GetPlatformDefault() || got.GetVersion() != "default/v1" || !proto.Equal(got.GetDocument().GetValue(), value(t, familiesDoc)) {
			t.Fatalf("没有自己词表的租户读到平台默认的：%+v（%v）", got, err)
		}

		h.mustPublish(t2, kindFamilies, "", ownFamilies)
		own, err := h.getFamilies(t2)
		if err != nil || own.GetPlatformDefault() || own.GetVersion() != "v1" || !proto.Equal(own.GetDocument().GetValue(), value(t, ownFamilies)) {
			t.Errorf("租户自己的词表优先：%+v（%v）", own, err)
		}
		if other, err := h.getFamilies(t1); err != nil || !other.GetPlatformDefault() {
			t.Errorf("t1 读不到 t2 的词表：%+v（%v）", other, err)
		}

		h.mustPublish(nil, kindFamilies, registry.DefaultTenantPolicy, familiesDocV2)
		if got, err := h.getFamilies(t1); err != nil || got.GetVersion() != "default/v2" {
			t.Errorf("平台默认的新版本立即生效：%+v（%v）", got, err)
		}
		_, err = h.promote(nil, kindFamilies, registry.DefaultTenantPolicy, "v1", passingEvidence)
		wantCode(t, "词表不走 Promote", err, connect.CodeInvalidArgument)
	})
}

func TestTaskFamiliesAreValidatedOnPublish(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		_, err := h.publish(nil, kindFamilies, "other", familiesDoc)
		wantCode(t, "不带租户的只能是 default", err, connect.CodeInvalidArgument)

		_, err = h.publish(t1, kindFamilies, "", `{"families":[{"id":"general.unclassified","description":"其他"}]}`)
		wantCode(t, "general.unclassified 是保留的", err, connect.CodeInvalidArgument)

		dup := `{"families":[{"id":"engineering.ci_fix","description":"a"},{"id":"engineering.ci_fix","description":"b"}]}`
		_, err = h.publish(t1, kindFamilies, "", dup)
		wantCode(t, "id 重复", err, connect.CodeInvalidArgument)
		if _, detail := errorDetail(t, err); detail.GetCode() != "duplicate_task_family" {
			t.Errorf("重复的 id 报 duplicate_task_family：%s", detail.GetCode())
		}
		_, err = h.getFamilies(t1)
		wantCode(t, "被拒的发布不留版本", err, connect.CodeNotFound)
	})
}
