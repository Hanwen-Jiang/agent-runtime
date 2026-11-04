package taskapi

import (
	"context"
	"fmt"

	"connectrpc.com/connect"
	"google.golang.org/protobuf/encoding/protojson"

	"github.com/agentruntime/agent-runtime/libs/go-core/providers/ingress"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	taskapiv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/taskapi/v1"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/ids"
)

const (
	routeSessionEvents = "list_session_events"
	// 一次最多取多少个 Task、每个 Task 每页默认与最多多少个事件（契约 task_api.proto 的 ListSessionEventsRequest）。
	maxSessionCursors = 50
	defaultEventPage  = 200
	maxEventPage      = 1000
)

// ListSessionEvents 一次取回一个 Session 里若干 Task 的事件（HIST-01）：每个 Task 按自己的游标读业务库里记下的事件
// （task_events，与 SSE 的 data 相同的 JSON），按 event_id 排序；结果与请求的 cursors 一一对应、顺序相同。别的租户、
// 别的 Session 的 Task 与不存在的一样：found 为假、没有事件。业务库里没有第 1 个事件的 Task（迁移之前开始的）标
// events_unavailable，客户端退回快照或 SSE。逐字的增量本来就不进库，这里也没有。
func (s *Server) ListSessionEvents(ctx context.Context, req *connect.Request[taskapiv1.ListSessionEventsRequest]) (*connect.Response[taskapiv1.ListSessionEventsResponse], error) {
	id, err := s.authenticate(ctx, req.Header(), routeSessionEvents)
	if err != nil {
		return nil, ConnectError(err)
	}
	m := req.Msg
	if !ids.ValidSessionID(m.GetSessionId()) {
		return nil, ConnectError(invalid("session_id 格式不对"))
	}
	page, err := eventPage(m.GetPageSize())
	if err != nil {
		return nil, ConnectError(err)
	}
	if err := checkCursors(m.GetCursors()); err != nil {
		return nil, ConnectError(err)
	}
	tenant, err := ingress.TenantContextFor(id, m.GetTenantId(), "", "", nil)
	if err != nil {
		return nil, ConnectError(err)
	}
	out := &taskapiv1.ListSessionEventsResponse{}
	for _, c := range m.GetCursors() {
		te, err := s.taskEvents(ctx, tenant, m.GetSessionId(), c, page)
		if err != nil {
			return nil, ConnectError(err)
		}
		out.Tasks = append(out.Tasks, te)
	}
	return connect.NewResponse(out), nil
}

func eventPage(n int32) (int, error) {
	switch {
	case n == 0:
		return defaultEventPage, nil
	case n < 0 || n > maxEventPage:
		return 0, invalid(fmt.Sprintf("page_size 在 1 到 %d 之间（0 为 %d）", maxEventPage, defaultEventPage))
	}
	return int(n), nil
}

func checkCursors(cursors []*taskapiv1.TaskEventCursor) error {
	if len(cursors) > maxSessionCursors {
		return invalid(fmt.Sprintf("一次最多取 %d 个 Task", maxSessionCursors))
	}
	seen := make(map[string]bool, len(cursors))
	for _, c := range cursors {
		switch {
		case !ids.ValidTaskID(c.GetTaskId()):
			return invalid("task_id 格式不对")
		case c.GetAfterEventId() < 0:
			return invalid("after_event_id 不能为负")
		case seen[c.GetTaskId()]:
			return invalid("同一个 Task 只能出现一次")
		}
		seen[c.GetTaskId()] = true
	}
	return nil
}

// taskEvents 读一个 Task 游标之后的一页事件。
func (s *Server) taskEvents(ctx context.Context, tenant *commonv1.TenantContext, sessionID string, c *taskapiv1.TaskEventCursor, page int) (*taskapiv1.TaskEvents, error) {
	te := &taskapiv1.TaskEvents{TaskId: c.GetTaskId(), NextAfterEventId: c.GetAfterEventId()}
	row, ok, err := s.tasks.LookupTask(ctx, tenant, c.GetTaskId())
	if err != nil || !ok || row.SessionID != sessionID {
		return te, err
	}
	te.Found = true
	complete, err := s.tasks.HasCompleteEvents(ctx, tenant, c.GetTaskId())
	if err != nil || !complete {
		te.EventsUnavailable = !complete
		return te, err
	}
	// 多取一个，看这一页之后还有没有。
	rows, err := s.tasks.ListEvents(ctx, tenant, c.GetTaskId(), c.GetAfterEventId(), page+1)
	if err != nil {
		return nil, err
	}
	if len(rows) > page {
		rows, te.More = rows[:page], true
	}
	for _, r := range rows {
		ev := &taskapiv1.TaskEvent{}
		if err := protojson.Unmarshal([]byte(r.Data), ev); err != nil {
			return nil, fmt.Errorf("taskapi: 业务库里 %s 的事件 %d 无法解码：%w", r.TaskID, r.EventID, err)
		}
		ev.EventId = r.EventID
		te.Events = append(te.Events, ev)
		te.NextAfterEventId = r.EventID
	}
	if te.Finished, err = s.tasks.HasFinishedEvent(ctx, tenant, c.GetTaskId()); err != nil {
		return nil, err
	}
	return te, nil
}
