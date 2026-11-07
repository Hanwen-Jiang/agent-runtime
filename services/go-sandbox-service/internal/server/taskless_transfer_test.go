package server_test

import (
	"context"
	"net/url"
	"strings"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/envcred"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/broker"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/ledger"
)

func TestTasklessTransferGrantsRemainLeaseScopedAndKeepQuota(t *testing.T) {
	for _, postgres := range []bool{false, true} {
		name := "sqlite"
		if postgres {
			name = "postgres"
		}
		t.Run(name, func(t *testing.T) {
			options := []option{withGateways("", "http://gateway.test:8095"), withPolicy("t1", map[string]any{"sandbox": map[string]any{"quota": map[string]any{"daily_egress_bytes": 987654}}})}
			if postgres {
				options = append(options, withPostgres())
			}
			h := newHarness(t, options...)
			ctx := context.Background()
			// Production has the Task Service schema. This fixture needs an
			// empty task catalog when it verifies reuse by a real task identity.
			if err := h.store.Migrate(ctx, []relational.Migration{{Version: 900, Name: "transfer_task_fixture", Statements: []string{`CREATE TABLE tasks (task_id TEXT PRIMARY KEY, submitted_by TEXT NOT NULL, personal_config_pin TEXT NOT NULL DEFAULT '')`}}}); err != nil {
				t.Fatal(err)
			}
			b := h.svc.Broker()
			open := func(owner, task string) (*broker.Lease, func() error) {
				t.Helper()
				tc := tenant(owner, "", "")
				lease := h.mustAcquire(tc, taskID(task), busyFor(time.Minute))
				inner, err := b.Get(ctx, tc, lease.LeaseId)
				if err != nil {
					t.Fatal(err)
				}
				beforeBusy := inner.Record.BusyUntil
				// The local Rust process supplies a real ready lease. Select the
				// gateway branch and empty Env to model restore before withEnv.
				inner.Kind = "k8s"
				inner.Env = nil
				proxy, close, err := b.OpenTransferNetwork(ctx, inner, "https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com/bucket/blob")
				if err != nil {
					t.Fatal(err)
				}
				rec, err := b.Ledger().Get(ctx, owner, inner.ID())
				if err != nil || rec == nil {
					t.Fatal("lease missing")
				}
				if rec.BusyUntil != beforeBusy {
					t.Fatal("binding transfer audit identity cleared busy protection")
				}
				if task != "" && rec.TaskID != task {
					t.Fatal("real task identity changed")
				}
				if task == "" && rec.TaskID != "workspace-transfer-"+inner.ID() {
					t.Fatalf("not lease scoped: %s", rec.TaskID)
				}
				parsed, err := url.Parse(proxy)
				if err != nil {
					t.Fatal("invalid generated proxy")
				}
				token, _ := parsed.User.Password()
				key := []byte("ZW52LWNyZWRlbnRpYWwta2V5LTAxMjM0NTY3ODlhYmNkZWY=")
				claims, err := envcred.Verify(key, token, envcred.PurposeEgress, h.clock.Now())
				if err != nil {
					t.Fatal("generated proxy credential invalid")
				}
				if claims.TenantID != owner || claims.EnvRef != inner.ID() {
					t.Fatal("proxy credential lost lease/tenant binding")
				}
				if owner == "t1" && claims.Limits["egress_bytes"] != 987654 {
					t.Fatalf("restore credential omitted quota: %v", claims.Limits)
				}
				return inner, close
			}
			first, closeFirst := open("t1", "")
			second, closeSecond := open("t1", "")
			third, closeThird := open("t2", "")
			if first.Record.TaskID == second.Record.TaskID || first.Record.TaskID == third.Record.TaskID {
				t.Fatal("internal transfer scopes shared an identity")
			}
			if err := closeFirst(); err != nil {
				t.Fatal(err)
			}
			if rows := h.rows("t1", "SELECT task_id FROM sandbox_grants WHERE state='active'"); len(rows) != 1 || rows[0]["task_id"] != second.Record.TaskID {
				t.Fatalf("close crossed lease scope: %v", rows)
			}
			if rows := h.rows("t2", "SELECT task_id FROM sandbox_grants WHERE state='active'"); len(rows) != 1 || rows[0]["task_id"] != third.Record.TaskID {
				t.Fatal("cross-tenant grant change")
			}
			if err := closeSecond(); err != nil {
				t.Fatal(err)
			}
			if err := closeThird(); err != nil {
				t.Fatal(err)
			}
			_, closeReal := open("t1", "real-task")
			if err := closeReal(); err != nil {
				t.Fatal(err)
			}
			// A stale pre-restore lease cannot overwrite a newer real Task.
			lease := h.mustAcquire(tenant("t1", "", ""))
			stale, err := b.Get(ctx, tenant("t1", "", ""), lease.LeaseId)
			if err != nil {
				t.Fatal(err)
			}
			stale.Kind = "k8s"
			stale.Env = nil
			if _, err = b.Ledger().Touch(ctx, *stale.Record, ledger.TouchArgs{TaskID: "new-real-task"}); err != nil {
				t.Fatal(err)
			}
			if _, _, err = b.OpenTransferNetwork(ctx, stale, "https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com/bucket/blob"); err == nil || !strings.Contains(err.Error(), "transfer_lease_changed") {
				t.Fatalf("stale task binding: %v", err)
			}
			if rows := h.rows("t1", "SELECT task_id FROM sandbox_grants WHERE state='active'"); len(rows) != 0 {
				t.Fatal("stale lease granted network")
			}
		})
	}
}

func TestTasklessTransferGrantIsRevokedBeforeRealTaskReuse(t *testing.T) {
	h := newHarness(t, withGateways("", "http://gateway.test:8095"))
	h.mkWorkspace("t1", "reuse-workspace", nil)
	tc := tenant("t1", "reuse-workspace", s1)
	first := h.mustAcquire(tc, onWorkspace("reuse-workspace"), reuse())
	b := h.svc.Broker()
	ctx := context.Background()
	inner, err := b.Get(ctx, tc, first.LeaseId)
	if err != nil {
		t.Fatal(err)
	}
	inner.Kind = "k8s"
	inner.Env = nil
	_, close, err := b.OpenTransferNetwork(ctx, inner, "https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com/bucket/blob")
	if err != nil {
		t.Fatal(err)
	}
	oldTask := inner.Record.TaskID
	if _, err = h.release(tc, first, true); err != nil {
		t.Fatal(err)
	}
	second := h.mustAcquire(tc, onWorkspace("reuse-workspace"), taskID("real-task"), reuse())
	if !second.Reused || second.LeaseId != first.LeaseId {
		t.Fatal("test did not reuse the original environment")
	}
	if second.Record.TaskId != "real-task" {
		t.Fatal("real task not bound")
	}
	if rows := h.rows("t1", "SELECT state FROM sandbox_grants WHERE task_id=$1", oldTask); len(rows) != 1 || rows[0]["state"] != "revoked" {
		t.Fatalf("old internal grant survived real Task reuse: %v", rows)
	}
	if err := close(); err != nil {
		t.Fatal(err)
	}
}
