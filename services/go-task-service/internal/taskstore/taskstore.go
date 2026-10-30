// Package taskstore 是 Task Service 在业务库里的两张表：Session 与 Task 的归属、提交去重、Task 的终态。
// 表按租户隔离由 RelationalStore 负责（providers/relational），这里的 SQL 不写租户条件。
//
// Task 事件的 event_id 也由这里分配（NextEventID）：业务库是持久的，Cache 重启后 event_id 也不回退；
// Task 的结局（TaskWorkflowResult 的 protojson）记在 outcome，Cache 里的推流丢了也能补发 task_finished（计划 S-07 场景 9）。
package taskstore

import (
	"context"
	"fmt"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

// Migrations 返回两张表的结构；版本 2 给 tasks 加上结局一列（S-07），版本 3 加上提交者（R-05 评审 D9：谁能审批与取消），
// 版本 4 加上命令原文（设计书 3.1.1 的“追加记录”：Worker 的 activity 据此读同一 Session 里之前的对话，这张表它只读）。
func Migrations() []relational.Migration {
	return []relational.Migration{{
		Version: 1,
		Name:    "task_service_sessions_and_tasks",
		Statements: []string{
			`CREATE TABLE sessions (
				session_id TEXT PRIMARY KEY,
				created_at BIGINT NOT NULL
			)`,
			`CREATE TABLE tasks (
				task_id TEXT PRIMARY KEY,
				session_id TEXT NOT NULL,
				session_key TEXT NOT NULL,
				client_request_id TEXT NOT NULL,
				status BIGINT NOT NULL,
				signaled BIGINT NOT NULL,
				last_event_id BIGINT NOT NULL,
				created_at BIGINT NOT NULL,
				UNIQUE (session_key, client_request_id)
			)`,
		},
	}, {
		Version: 2,
		Name:    "task_service_task_outcome",
		Statements: []string{
			`ALTER TABLE tasks ADD COLUMN outcome TEXT NOT NULL DEFAULT ''`,
		},
	}, {
		Version: 3,
		Name:    "task_service_task_submitter",
		Statements: []string{
			`ALTER TABLE tasks ADD COLUMN submitted_by TEXT NOT NULL DEFAULT ''`,
		},
	}, {
		Version: 4,
		Name:    "task_service_task_message",
		Statements: []string{
			`ALTER TABLE tasks ADD COLUMN message TEXT NOT NULL DEFAULT ''`,
		},
	}}
}

// Task 是一行 Task 记录。
type Task struct {
	TaskID    string
	SessionID string
	// SessionKey 是提交时请求里的 session_id，新建 Session 时为空；与 ClientRequestID 一起去重。
	SessionKey      string
	ClientRequestID string
	// Status 只记终态；还没结束时为 UNSPECIFIED，实际状态以工作流为准。
	Status commonv1.TaskStatus
	// Signaled 表示提交信号已经送达 Session 工作流。
	Signaled    bool
	LastEventID int64
	CreatedAt   time.Time
	// Outcome 是 Task 结束时的 TaskWorkflowResult（protojson，proto 字段名）；还没结束时为空。
	Outcome string
	// SubmittedBy 是提交者的 user_id（取自认证身份）；迁移 3 之前提交的 Task 为空。
	SubmittedBy string
	// Message 是提交时的命令原文；迁移 4 之前提交的 Task 为空。
	Message string
}

// Finished 表示业务库里已记下终态与结局。
func (t Task) Finished() bool {
	return t.Status != commonv1.TaskStatus_TASK_STATUS_UNSPECIFIED && t.Outcome != ""
}

// Store 读写这两张表。每个方法都按参数里的租户绑定 RelationalStore。
type Store struct{ p relational.Provider }

// New 构造 Store。
func New(p relational.Provider) *Store { return &Store{p: p} }

func (s *Store) tx(ctx context.Context, tenant *commonv1.TenantContext, readOnly bool, fn func(context.Context, relational.Tx) error) error {
	store, err := s.p.Bind(tenant)
	if err != nil {
		return err
	}
	return store.Transaction(ctx, relational.TxOptions{ReadOnly: readOnly}, fn)
}

// CreateSession 登记一个新 Session；已存在时不变。
func (s *Store) CreateSession(ctx context.Context, tenant *commonv1.TenantContext, sessionID string, at time.Time) error {
	return s.tx(ctx, tenant, false, func(ctx context.Context, tx relational.Tx) error {
		_, err := tx.Execute(ctx, `INSERT INTO sessions (session_id, created_at) VALUES ($1, $2) ON CONFLICT DO NOTHING`,
			sessionID, at.UnixMilli())
		return err
	})
}

// SessionExists 返回该租户下 Session 是否存在。
func (s *Store) SessionExists(ctx context.Context, tenant *commonv1.TenantContext, sessionID string) (bool, error) {
	var ok bool
	err := s.tx(ctx, tenant, true, func(ctx context.Context, tx relational.Tx) error {
		_, found, err := tx.QueryOne(ctx, `SELECT session_id FROM sessions WHERE session_id = $1`, sessionID)
		ok = found
		return err
	})
	return ok, err
}

const taskColumns = `task_id, session_id, session_key, client_request_id, status, signaled, last_event_id, created_at, outcome, submitted_by, message`

// InsertTask 插入一行；（SessionKey, ClientRequestID）已存在时不插入，返回已有的那一行与 true。
func (s *Store) InsertTask(ctx context.Context, tenant *commonv1.TenantContext, t Task) (Task, bool, error) {
	var out Task
	var existed bool
	err := s.tx(ctx, tenant, false, func(ctx context.Context, tx relational.Tx) error {
		n, err := tx.Execute(ctx, `INSERT INTO tasks (`+taskColumns+`) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) ON CONFLICT DO NOTHING`,
			t.TaskID, t.SessionID, t.SessionKey, t.ClientRequestID, int64(t.Status), boolInt(t.Signaled), t.LastEventID, t.CreatedAt.UnixMilli(), t.Outcome, t.SubmittedBy, t.Message)
		if err != nil {
			return err
		}
		if n == 1 {
			out = t
			return nil
		}
		row, found, err := tx.QueryOne(ctx, `SELECT `+taskColumns+` FROM tasks WHERE session_key = $1 AND client_request_id = $2`,
			t.SessionKey, t.ClientRequestID)
		if err != nil {
			return err
		}
		if !found {
			return fmt.Errorf("taskstore: task_id %s 冲突但找不到同一提交的记录", t.TaskID)
		}
		out, existed = decode(row), true
		return nil
	})
	return out, existed, err
}

// LookupTask 按 task_id 读取；不存在或属于别的租户时 ok 为 false。
func (s *Store) LookupTask(ctx context.Context, tenant *commonv1.TenantContext, taskID string) (Task, bool, error) {
	var out Task
	var ok bool
	err := s.tx(ctx, tenant, true, func(ctx context.Context, tx relational.Tx) error {
		row, found, err := tx.QueryOne(ctx, `SELECT `+taskColumns+` FROM tasks WHERE task_id = $1`, taskID)
		if err != nil || !found {
			return err
		}
		out, ok = decode(row), true
		return nil
	})
	return out, ok, err
}

// MarkSignaled 记下提交信号已经送达。
func (s *Store) MarkSignaled(ctx context.Context, tenant *commonv1.TenantContext, taskID string) error {
	return s.tx(ctx, tenant, false, func(ctx context.Context, tx relational.Tx) error {
		_, err := tx.Execute(ctx, `UPDATE tasks SET signaled = 1 WHERE task_id = $1`, taskID)
		return err
	})
}

// RecordFinished 记下终态、结局（TaskWorkflowResult 的 protojson）与最后一个事件的 id。
func (s *Store) RecordFinished(ctx context.Context, tenant *commonv1.TenantContext, taskID string, status commonv1.TaskStatus, lastEventID int64, outcome string) error {
	return s.tx(ctx, tenant, false, func(ctx context.Context, tx relational.Tx) error {
		_, err := tx.Execute(ctx, `UPDATE tasks SET status = $1, outcome = $2,
			last_event_id = CASE WHEN last_event_id < $3 THEN $3 ELSE last_event_id END
			WHERE task_id = $4`, int64(status), outcome, lastEventID, taskID)
		return err
	})
}

// NextEventID 给 Task 的下一个事件分配 event_id：在业务库里加一，从 1 开始严格递增，Cache 重启也不回退。
// Task 不存在（或属于别的租户）时报 not_found。
func (s *Store) NextEventID(ctx context.Context, tenant *commonv1.TenantContext, taskID string) (int64, error) {
	var id int64
	err := s.tx(ctx, tenant, false, func(ctx context.Context, tx relational.Tx) error {
		n, err := tx.Execute(ctx, `UPDATE tasks SET last_event_id = last_event_id + 1 WHERE task_id = $1`, taskID)
		if err != nil {
			return err
		}
		if n != 1 {
			return providererr.New(providererr.NotFound, "task_not_found", "没有这个 Task 的记录")
		}
		row, _, err := tx.QueryOne(ctx, `SELECT last_event_id FROM tasks WHERE task_id = $1`, taskID)
		if err != nil {
			return err
		}
		id, _ = row["last_event_id"].(int64)
		return nil
	})
	return id, err
}

func boolInt(b bool) int64 {
	if b {
		return 1
	}
	return 0
}

func decode(row relational.Row) Task {
	str := func(k string) string { s, _ := row[k].(string); return s }
	num := func(k string) int64 { n, _ := row[k].(int64); return n }
	return Task{
		TaskID:          str("task_id"),
		SessionID:       str("session_id"),
		SessionKey:      str("session_key"),
		ClientRequestID: str("client_request_id"),
		Status:          commonv1.TaskStatus(num("status")),
		Signaled:        num("signaled") != 0,
		LastEventID:     num("last_event_id"),
		CreatedAt:       time.UnixMilli(num("created_at")).UTC(),
		Outcome:         str("outcome"),
		SubmittedBy:     str("submitted_by"),
		Message:         str("message"),
	}
}
