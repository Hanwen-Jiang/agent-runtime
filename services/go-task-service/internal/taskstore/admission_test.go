package taskstore_test

import (
	"context"
	"testing"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/taskstore"
)

func TestAdmissionLimitUsesProviderTenantScope(t *testing.T) {
	eachBackend(t, func(t *testing.T, s *taskstore.Store, _ relational.Provider) {
		ctx := context.Background()
		task := taskstore.Task{TaskID: "tsk_first", SessionID: "ses_first", ClientRequestID: "one", CreatedAt: now}
		if _, _, err := s.InsertTaskWithLimit(ctx, t1, task, 1); err != nil {
			t.Fatal(err)
		}
		if _, duplicate, err := s.InsertTaskWithLimit(ctx, t1, task, 1); err != nil || !duplicate {
			t.Fatalf("idempotent retry at capacity: duplicate=%v err=%v", duplicate, err)
		}
		next := task
		next.TaskID, next.ClientRequestID = "tsk_second", "two"
		if _, _, err := s.InsertTaskWithLimit(ctx, t1, next, 1); providererr.CategoryOf(err) != providererr.Denied {
			t.Fatalf("second admission must be denied: %v", err)
		}
		if _, _, err := s.InsertTaskWithLimit(ctx, t2, next, 1); err != nil {
			t.Fatalf("other tenant has its own capacity: %v", err)
		}
	})
}

func TestAdmissionLockUpgradePreservesTasks(t *testing.T) {
	for _, b := range backends {
		t.Run(b.name, func(t *testing.T) {
			p := b.open(t)
			ctx := context.Background()
			migrations := taskstore.Migrations()
			if err := p.Migrate(ctx, migrations[:5]); err != nil {
				t.Fatal(err)
			}
			s := taskstore.New(p)
			insert(t, s, t1, "tsk_before", "before")
			if err := p.Migrate(ctx, migrations); err != nil {
				t.Fatal(err)
			}
			task := taskstore.Task{TaskID: "tsk_after", SessionID: "ses_after", ClientRequestID: "after", CreatedAt: now}
			if _, _, err := s.InsertTaskWithLimit(ctx, t1, task, 2); err != nil {
				t.Fatal(err)
			}
			if _, ok, err := s.LookupTask(ctx, t1, "tsk_before"); err != nil || !ok {
				t.Fatalf("upgrade lost business record: ok=%v err=%v", ok, err)
			}
		})
	}
}
