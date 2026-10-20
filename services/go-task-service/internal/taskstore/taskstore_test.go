package taskstore_test

// S-12a 验收：业务库里 Task Service 的两张表按租户隔离（计划 S-12a“以 A 租户身份读写 B 租户的数据，全部被拒或返回空”）。
// 同一组用例分别跑在开发实现（SQLite，每个作用域一个库）与生产实现（Postgres，行级安全）上。

import (
	"context"
	"os"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/postgres"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/postgres/pgtest"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/sqlitestore"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/taskstore"
)

func TestMain(m *testing.M) { os.Exit(pgtest.Main(m)) }

var (
	t1  = &commonv1.TenantContext{TenantId: "t1"}
	t2  = &commonv1.TenantContext{TenantId: "t2"}
	now = time.Unix(1_790_000_000, 0).UTC()
)

type backend struct {
	name string
	open func(t *testing.T) relational.Provider
}

var backends = []backend{
	{"sqlite", func(t *testing.T) relational.Provider {
		p, err := sqlitestore.New(sqlitestore.Options{Dir: t.TempDir()})
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = p.Close() })
		return p
	}},
	{"postgres", func(t *testing.T) relational.Provider {
		c := pgtest.Current(t)
		p, err := postgres.New(context.Background(), postgres.Options{DSN: c.DSN(pgtest.AppRole, c.NewDatabase(t))})
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = p.Close() })
		return p
	}},
}

func eachBackend(t *testing.T, fn func(t *testing.T, store *taskstore.Store, p relational.Provider)) {
	for _, b := range backends {
		t.Run(b.name, func(t *testing.T) {
			p := b.open(t)
			if err := p.Migrate(context.Background(), taskstore.Migrations()); err != nil {
				t.Fatal(err)
			}
			fn(t, taskstore.New(p), p)
		})
	}
}

func insert(t *testing.T, s *taskstore.Store, tenant *commonv1.TenantContext, id, crid string) (taskstore.Task, bool) {
	t.Helper()
	row, existed, err := s.InsertTask(context.Background(), tenant, taskstore.Task{TaskID: id, SessionID: "ses_" + id, ClientRequestID: crid, CreatedAt: now})
	if err != nil {
		t.Fatal(err)
	}
	return row, existed
}

func TestTasksAreInvisibleToOtherTenants(t *testing.T) {
	eachBackend(t, func(t *testing.T, s *taskstore.Store, _ relational.Provider) {
		ctx := context.Background()
		insert(t, s, t1, "tsk_a", "c1")
		if _, ok, err := s.LookupTask(ctx, t2, "tsk_a"); err != nil || ok {
			t.Errorf("t2 查不到 t1 的 Task：ok=%v err=%v", ok, err)
		}
		if _, err := s.NextEventID(ctx, t2, "tsk_a"); providererr.CategoryOf(err) != providererr.NotFound {
			t.Errorf("t2 不能给 t1 的 Task 分配事件 ID，应报 NotFound，实际 %v", err)
		}
		if err := s.RecordFinished(ctx, t2, "tsk_a", commonv1.TaskStatus_TASK_STATUS_DONE, 9, `{"status":"TASK_STATUS_DONE"}`); err != nil {
			t.Fatal(err)
		}
		if err := s.MarkSignaled(ctx, t2, "tsk_a"); err != nil {
			t.Fatal(err)
		}
		row, ok, err := s.LookupTask(ctx, t1, "tsk_a")
		if err != nil || !ok || row.Finished() || row.Signaled || row.LastEventID != 0 {
			t.Errorf("t2 的写入不能改到 t1 的 Task：%+v ok=%v err=%v", row, ok, err)
		}
	})
}

func TestTheSameClientRequestIDInTwoTenantsCreatesTwoTasks(t *testing.T) {
	eachBackend(t, func(t *testing.T, s *taskstore.Store, _ relational.Provider) {
		a, existed := insert(t, s, t1, "tsk_a", "dup")
		if existed {
			t.Fatal("t1 第一次提交不是重复")
		}
		b, existed := insert(t, s, t2, "tsk_b", "dup")
		if existed || b.TaskID != "tsk_b" {
			t.Errorf("t2 用同一个 client_request_id 提交是另一个 Task，不能去重到 t1 的：%+v existed=%v", b, existed)
		}
		again, existed := insert(t, s, t1, "tsk_c", "dup")
		if !existed || again.TaskID != a.TaskID {
			t.Errorf("t1 重复提交仍返回自己的 Task：%+v existed=%v", again, existed)
		}
	})
}

func TestSessionsAreScoped(t *testing.T) {
	eachBackend(t, func(t *testing.T, s *taskstore.Store, _ relational.Provider) {
		ctx := context.Background()
		if err := s.CreateSession(ctx, t1, "ses_shared", now); err != nil {
			t.Fatal(err)
		}
		if ok, err := s.SessionExists(ctx, t2, "ses_shared"); err != nil || ok {
			t.Errorf("t2 看不到 t1 的 Session：ok=%v err=%v", ok, err)
		}
		if ok, err := s.SessionExists(ctx, t1, "ses_shared"); err != nil || !ok {
			t.Errorf("t1 看得到自己的 Session：ok=%v err=%v", ok, err)
		}
	})
}

func TestTaskServiceTablesAreCoveredByRowLevelSecurity(t *testing.T) {
	p := backends[1].open(t).(*postgres.Provider)
	if err := p.Migrate(context.Background(), taskstore.Migrations()); err != nil {
		t.Fatal(err)
	}
	audits, err := p.AuditRLS(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(audits) < 2 {
		t.Fatalf("应当核对 sessions 与 tasks 两张表，实际 %+v", audits)
	}
	for _, a := range audits {
		if !a.Covered() {
			t.Errorf("表 %s 的租户隔离不完整：%+v", a.Table, a)
		}
	}
}
