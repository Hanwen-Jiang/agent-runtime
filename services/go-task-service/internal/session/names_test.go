package session

// S-03 验收：Go 的 Session 工作流与 Python 的 Task 工作流靠同一组名字对上，名字只在契约里定义一次
// （names.proto 的 wire_name），这里逐个比对。

import (
	"testing"

	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"

	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	workflowv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workflow/v1"
)

func wireName(t *testing.T, enum, value string) string {
	t.Helper()
	e := workflowv1.File_agentruntime_workflow_v1_names_proto.Enums().ByName(protoreflect.Name(enum))
	if e == nil {
		t.Fatalf("names.proto 中没有枚举 %s", enum)
	}
	v := e.Values().ByName(protoreflect.Name(value))
	if v == nil {
		t.Fatalf("契约中还没有 %s.%s：需要按 S-01 流程加入 names.proto", enum, value)
	}
	name, _ := proto.GetExtension(v.Options(), commonv1.E_WireName).(string)
	if name == "" {
		t.Fatalf("%s.%s 没有 wire_name", enum, value)
	}
	return name
}

func TestNamesMatchContract(t *testing.T) {
	cases := []struct{ enum, value, got string }{
		{"WorkflowType", "WORKFLOW_TYPE_SESSION", WorkflowName},
		{"WorkflowType", "WORKFLOW_TYPE_TASK", TaskWorkflowName},
		{"SignalName", "SIGNAL_NAME_SUBMIT_TASK", SignalSubmitTask},
		{"SignalName", "SIGNAL_NAME_CANCEL_TASK", SignalCancelTask},
		{"SignalName", "SIGNAL_NAME_TASK_FINISHED", SignalTaskFinished},
		{"QueryName", "QUERY_NAME_SESSION_STATE", QuerySessionState},
		{"QueryName", "QUERY_NAME_TASK_STATE", QueryTaskState},
		{"SignalName", "SIGNAL_NAME_HUMAN_REPLY", SignalHumanReply},
		{"TaskQueue", "TASK_QUEUE_SESSION", SessionTaskQueue},
		{"TaskQueue", "TASK_QUEUE_TASK", TaskTaskQueue},
		// T-11b：Task 工作流推送进度事件的跨语言 activity。
		{"ActivityName", "ACTIVITY_NAME_PUBLISH_TASK_EVENT", PublishTaskEventActivity},
	}
	for _, c := range cases {
		t.Run(c.value, func(t *testing.T) {
			if want := wireName(t, c.enum, c.value); c.got != want {
				t.Errorf("%s 应为契约里的 %q，实际 %q", c.value, want, c.got)
			}
		})
	}
}
