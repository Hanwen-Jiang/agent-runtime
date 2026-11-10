// Package taskstore 是 Task Service 在业务库里的表：Session 与 Task 的归属、提交去重、Task 的终态，以及 Task 的事件（HIST-01）。
// 表按租户隔离由 RelationalStore 负责（providers/relational），这里的 SQL 不写租户条件。
//
// Task 事件的 event_id 也由这里分配（NextEventID）：业务库是持久的，Cache 重启后 event_id 也不回退；
// Task 的结局（TaskWorkflowResult 的 protojson）记在 outcome，Cache 里的推流丢了也能补发 task_finished（计划 S-07 场景 9）。
package taskstore

import (
	"context"
	"encoding/json"
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
	}, {
		Version: 5,
		Name:    "task_service_task_admission_lock",
		Statements: []string{
			`CREATE TABLE task_admission_lock (
				tenant_id TEXT NOT NULL,
				lock_id BIGINT NOT NULL,
				touched_at BIGINT NOT NULL,
				PRIMARY KEY (tenant_id, lock_id)
			)`,
		},
	}, {
		Version: 6,
		Name:    "task_service_scoped_admission_lock",
		Statements: []string{
			// Lock rows contain no business data. Recreate the v5 table so SQLite
			// and Postgres both let the provider own the tenant discriminator.
			`DROP TABLE task_admission_lock`,
			`CREATE TABLE task_admission_lock (
				lock_id BIGINT PRIMARY KEY,
				touched_at BIGINT NOT NULL
			)`,
		},
	}, {
		Version: 7,
		Name:    "task_service_task_events",
		Statements: []string{
			// HIST-01：Task 的事件按 event_id 只追加地记下来（Session 的 activity 先写这里、再写 Cache 的推流），推流丢了也读得到。
			// dedupe_key 是推送的去重键（事件类型或 Task 工作流给的键）：activity 重试时据此沿用原来的 event_id。
			`CREATE TABLE task_events (
				task_id TEXT NOT NULL,
				event_id BIGINT NOT NULL,
				dedupe_key TEXT NOT NULL,
				session_id TEXT NOT NULL,
				event_type TEXT NOT NULL,
				data TEXT NOT NULL,
				created_at BIGINT NOT NULL,
				PRIMARY KEY (task_id, event_id),
				UNIQUE (task_id, dedupe_key)
			)`,
		},
	}, {
		Version: 8,
		Name:    "task_service_task_skill_pins",
		Statements: []string{
			// N-14：提交时固定的 Skill（名字 → <层>/vN 的 JSON；没有时为空）。Worker 的 Planner 目录与 skill_read 按 task_id 读，只读。
			`ALTER TABLE tasks ADD COLUMN skill_pins TEXT NOT NULL DEFAULT ''`,
		},
	}, {
		Version: 9,
		Name:    "task_service_submission_budget",
		Statements: []string{
			`ALTER TABLE tasks ADD COLUMN submission_hash TEXT NOT NULL DEFAULT ''`,
			`ALTER TABLE tasks ADD COLUMN submission_budget TEXT NOT NULL DEFAULT ''`,
		},
	}}
}

// SetSkillPins 记下 Task 固定的 Skill（N-14）；pins 为空时写空串。
func (s *Store) SetSkillPins(ctx context.Context, tenant *commonv1.TenantContext, taskID string, pins map[string]string) error {
	text := ""
	if len(pins) > 0 {
		raw, err := json.Marshal(pins)
		if err != nil {
			return err
		}
		text = string(raw)
	}
	return s.tx(ctx, tenant, false, func(ctx context.Context, tx relational.Tx) error {
		_, err := tx.Execute(ctx, `UPDATE tasks SET skill_pins = $1 WHERE task_id = $2`, text, taskID)
		return err
	})
}

// Event 是业务库里记下的一个 Task 事件；Data 是 TaskEvent 的 protojson（proto 字段名，与推流里的一样，含 event_id）。
type Event struct {
	TaskID    string
	EventID   int64
	DedupeKey string
	SessionID string
	Type      string
	Data      string
	CreatedAt time.Time
}

// RecordEvent 记下一个事件。同一个 Task 同一个去重键已经记过时不再写，返回已有的那条（activity 重试沿用它的 event_id）。
func (s *Store) RecordEvent(ctx context.Context, tenant *commonv1.TenantContext, ev Event) (Event, error) {
	var out Event
	err := s.tx(ctx, tenant, false, func(ctx context.Context, tx relational.Tx) error {
		if _, err := tx.Execute(ctx, `INSERT INTO task_events (task_id, event_id, dedupe_key, session_id, event_type, data, created_at)
			VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (task_id, dedupe_key) DO NOTHING`,
			ev.TaskID, ev.EventID, ev.DedupeKey, ev.SessionID, ev.Type, ev.Data, ev.CreatedAt.UnixMilli()); err != nil {
			return err
		}
		row, found, err := tx.QueryOne(ctx, `SELECT `+eventColumns+` FROM task_events WHERE task_id = $1 AND dedupe_key = $2`, ev.TaskID, ev.DedupeKey)
		if err == nil && found {
			out = decodeEvent(row)
		}
		return err
	})
	return out, err
}

// EventByKey 读同一个去重键已经记下的事件；没有时 ok 为 false。
func (s *Store) EventByKey(ctx context.Context, tenant *commonv1.TenantContext, taskID, key string) (Event, bool, error) {
	var out Event
	var ok bool
	err := s.tx(ctx, tenant, true, func(ctx context.Context, tx relational.Tx) error {
		row, found, err := tx.QueryOne(ctx, `SELECT `+eventColumns+` FROM task_events WHERE task_id = $1 AND dedupe_key = $2`, taskID, key)
		if err == nil && found {
			out, ok = decodeEvent(row), true
		}
		return err
	})
	return out, ok, err
}

// ListEvents 按 event_id 读 after 之后的至多 limit 个事件（早的在前）。别的租户的 Task 读不到（RelationalStore 按租户隔离）。
func (s *Store) ListEvents(ctx context.Context, tenant *commonv1.TenantContext, taskID string, after int64, limit int) ([]Event, error) {
	var out []Event
	err := s.tx(ctx, tenant, true, func(ctx context.Context, tx relational.Tx) error {
		rows, err := tx.Query(ctx, `SELECT `+eventColumns+` FROM task_events WHERE task_id = $1 AND event_id > $2 ORDER BY event_id LIMIT $3`, taskID, after, limit)
		for _, row := range rows {
			out = append(out, decodeEvent(row))
		}
		return err
	})
	return out, err
}

// HasCompleteEvents 表示业务库里记着这个 Task 的第一个事件（task_accepted，event_id 1）：之后的事件都会先记进业务库，
// 所以它是完整的。迁移 7 之前开始的 Task 没有，读事件时退回 Cache 的推流。
func (s *Store) HasCompleteEvents(ctx context.Context, tenant *commonv1.TenantContext, taskID string) (bool, error) {
	var ok bool
	err := s.tx(ctx, tenant, true, func(ctx context.Context, tx relational.Tx) error {
		_, found, err := tx.QueryOne(ctx, `SELECT event_id FROM task_events WHERE task_id = $1 AND event_id = 1`, taskID)
		ok = found
		return err
	})
	return ok, err
}

// HasFinishedEvent 表示业务库里已经记着这个 Task 的 task_finished（HIST-01 的批量取用它标 finished）。
func (s *Store) HasFinishedEvent(ctx context.Context, tenant *commonv1.TenantContext, taskID string) (bool, error) {
	var ok bool
	err := s.tx(ctx, tenant, true, func(ctx context.Context, tx relational.Tx) error {
		_, found, err := tx.QueryOne(ctx, `SELECT event_id FROM task_events WHERE task_id = $1 AND event_type = $2`, taskID, "task_finished")
		ok = found
		return err
	})
	return ok, err
}

const eventColumns = `task_id, event_id, dedupe_key, session_id, event_type, data, created_at`

func decodeEvent(row relational.Row) Event {
	str := func(k string) string { s, _ := row[k].(string); return s }
	num := func(k string) int64 { n, _ := row[k].(int64); return n }
	return Event{
		TaskID: str("task_id"), EventID: num("event_id"), DedupeKey: str("dedupe_key"), SessionID: str("session_id"),
		Type: str("event_type"), Data: str("data"), CreatedAt: time.UnixMilli(num("created_at")).UTC(),
	}
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
	// SubmissionHash pins the complete client request; pre-v9 rows remain empty.
	SubmissionHash string
	// SubmissionBudget is the first admission's effective Budget (protojson).
	SubmissionBudget string
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

const taskColumns = `task_id, session_id, session_key, client_request_id, status, signaled, last_event_id, created_at, outcome, submitted_by, message, submission_hash, submission_budget`

// SubmissionExists exempts retries of an already accepted request from new
// budget admission. InsertTask still owns atomic request deduplication.
func (s *Store) SubmissionExists(ctx context.Context, tenant *commonv1.TenantContext, sessionKey, requestID string) (bool, error) {
	var found bool
	err := s.tx(ctx, tenant, true, func(ctx context.Context, tx relational.Tx) error {
		_, ok, err := tx.QueryOne(ctx, `SELECT task_id FROM tasks WHERE session_key=$1 AND client_request_id=$2`, sessionKey, requestID)
		found = ok
		return err
	})
	return found, err
}

// LookupSubmission resolves an idempotency key without creating a Task. It is
// used only to choose frozen admission scope on retries; the caller must verify
// SubmittedBy before treating this as the authenticated user's request.
func (s *Store) LookupSubmission(ctx context.Context, tenant *commonv1.TenantContext, sessionKey, requestID string) (Task, bool, error) {
	var out Task
	var found bool
	err := s.tx(ctx, tenant, true, func(ctx context.Context, tx relational.Tx) error {
		row, ok, e := tx.QueryOne(ctx, `SELECT `+taskColumns+` FROM tasks WHERE session_key=$1 AND client_request_id=$2`, sessionKey, requestID)
		if e != nil {
			return e
		}
		found = ok
		if ok {
			out = decode(row)
		}
		return nil
	})
	return out, found, err
}

// InsertTask 插入一行；（SessionKey, ClientRequestID）已存在时不插入，返回已有的那一行与 true。
func (s *Store) InsertTask(ctx context.Context, tenant *commonv1.TenantContext, t Task) (Task, bool, error) {
	return s.insertTask(ctx, tenant, t, 0)
}

// InsertTaskWithLimit 在插入前原子地限制本租户未终态 Task 数量。
// task_admission_lock 是每个租户作用域里的一行锁：SQLite 的写事务已经串行化，Postgres 则用这行的 UPDATE
// 串行化同一租户的提交，避免并发 count 后同时插入超过上限。重复提交先返回原 Task，不消耗准入额度。
func (s *Store) InsertTaskWithLimit(ctx context.Context, tenant *commonv1.TenantContext, t Task, maxActive int) (Task, bool, error) {
	if maxActive <= 0 {
		return s.insertTask(ctx, tenant, t, 0)
	}
	return s.insertTask(ctx, tenant, t, maxActive)
}

func (s *Store) insertTask(ctx context.Context, tenant *commonv1.TenantContext, t Task, maxActive int) (Task, bool, error) {
	var out Task
	var existed bool
	err := s.tx(ctx, tenant, false, func(ctx context.Context, tx relational.Tx) error {
		if maxActive > 0 {
			if _, err := tx.Execute(ctx, `INSERT INTO task_admission_lock (lock_id, touched_at) VALUES (1, $1) ON CONFLICT DO NOTHING`, t.CreatedAt.UnixMilli()); err != nil {
				return err
			}
			if _, err := tx.Execute(ctx, `UPDATE task_admission_lock SET touched_at = touched_at WHERE lock_id = 1`); err != nil {
				return err
			}
		}
		// 先查重复提交：达到上限时，重试同一请求仍应返回原 Task。
		row, found, err := tx.QueryOne(ctx, `SELECT `+taskColumns+` FROM tasks WHERE session_key = $1 AND client_request_id = $2`,
			t.SessionKey, t.ClientRequestID)
		if err != nil {
			return err
		}
		if found {
			out, existed = decode(row), true
			return nil
		}
		if maxActive > 0 {
			row, _, err := tx.QueryOne(ctx, `SELECT COUNT(*) AS active_count FROM tasks WHERE status = $1`, int64(commonv1.TaskStatus_TASK_STATUS_UNSPECIFIED))
			if err != nil {
				return err
			}
			active, _ := row["active_count"].(int64)
			if active >= int64(maxActive) {
				return providererr.New(providererr.Denied, "tenant_busy", fmt.Sprintf("本租户已有 %d 个未完成的 Task", active))
			}
		}
		n, err := tx.Execute(ctx, `INSERT INTO tasks (`+taskColumns+`) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13) ON CONFLICT DO NOTHING`,
			t.TaskID, t.SessionID, t.SessionKey, t.ClientRequestID, int64(t.Status), boolInt(t.Signaled), t.LastEventID, t.CreatedAt.UnixMilli(), t.Outcome, t.SubmittedBy, t.Message, t.SubmissionHash, t.SubmissionBudget)
		if err != nil {
			return err
		}
		if n == 1 {
			out = t
			return nil
		}
		row, found, err = tx.QueryOne(ctx, `SELECT `+taskColumns+` FROM tasks WHERE session_key = $1 AND client_request_id = $2`,
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

// SessionTasks 读一个 Session 里最近的 limit 个 Task，按提交的先后（早的在前）；more 表示更早的还有。
// 会话快照用（N-19）。别的租户的 Session 读不到（RelationalStore 按租户隔离）。
func (s *Store) SessionTasks(ctx context.Context, tenant *commonv1.TenantContext, sessionID string, limit int) (tasks []Task, more bool, err error) {
	err = s.tx(ctx, tenant, true, func(ctx context.Context, tx relational.Tx) error {
		rows, err := tx.Query(ctx, `SELECT `+taskColumns+` FROM tasks WHERE session_id = $1 ORDER BY created_at DESC, task_id DESC LIMIT $2`, sessionID, limit+1)
		if err != nil {
			return err
		}
		if len(rows) > limit {
			more, rows = true, rows[:limit]
		}
		tasks = make([]Task, len(rows))
		for i, row := range rows {
			tasks[len(rows)-1-i] = decode(row)
		}
		return nil
	})
	return tasks, more, err
}

// MarkSignaled 记下提交信号已经送达。
func (s *Store) MarkSignaled(ctx context.Context, tenant *commonv1.TenantContext, taskID string) error {
	return s.tx(ctx, tenant, false, func(ctx context.Context, tx relational.Tx) error {
		_, err := tx.Execute(ctx, `UPDATE tasks SET signaled = 1 WHERE task_id = $1`, taskID)
		return err
	})
}

// UpdateMessage 改排队中的 Task 的消息（docs/plan/running-task-interaction.md 第 2 节）。
func (s *Store) UpdateMessage(ctx context.Context, tenant *commonv1.TenantContext, taskID, message string) error {
	return s.tx(ctx, tenant, false, func(ctx context.Context, tx relational.Tx) error {
		_, err := tx.Execute(ctx, `UPDATE tasks SET message = $1 WHERE task_id = $2`, message, taskID)
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
		TaskID:           str("task_id"),
		SessionID:        str("session_id"),
		SessionKey:       str("session_key"),
		ClientRequestID:  str("client_request_id"),
		Status:           commonv1.TaskStatus(num("status")),
		Signaled:         num("signaled") != 0,
		LastEventID:      num("last_event_id"),
		CreatedAt:        time.UnixMilli(num("created_at")).UTC(),
		Outcome:          str("outcome"),
		SubmittedBy:      str("submitted_by"),
		Message:          str("message"),
		SubmissionHash:   str("submission_hash"),
		SubmissionBudget: str("submission_budget"),
	}
}
