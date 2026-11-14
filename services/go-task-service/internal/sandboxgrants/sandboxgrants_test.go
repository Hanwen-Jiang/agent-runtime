package sandboxgrants

import (
	"context"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/dev/sqlitestore"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

func grantStore(t *testing.T) relational.Provider {
	t.Helper()
	store, err := sqlitestore.New(sqlitestore.Options{Dir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = store.Close() })
	err = store.Migrate(t.Context(), []relational.Migration{{Version: 202, Name: "sandbox_grants", Statements: []string{
		`CREATE TABLE sandbox_grants (
 grant_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, workspace_id TEXT NOT NULL DEFAULT '', session_id TEXT NOT NULL DEFAULT '',
 scope TEXT NOT NULL DEFAULT 'hosts', hosts TEXT NOT NULL, ports TEXT NOT NULL, expires_at TEXT NOT NULL,
 max_up_bytes BIGINT NOT NULL DEFAULT 0, max_down_bytes BIGINT NOT NULL DEFAULT 0, max_conns BIGINT NOT NULL DEFAULT 0, max_rps BIGINT NOT NULL DEFAULT 0,
 up_bytes BIGINT NOT NULL DEFAULT 0, down_bytes BIGINT NOT NULL DEFAULT 0, preview_port BIGINT NOT NULL DEFAULT 0,
 state TEXT NOT NULL, approved_by TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
 revoked_at TEXT NOT NULL DEFAULT '', preview_env TEXT NOT NULL DEFAULT '', preview_target TEXT NOT NULL DEFAULT '')`,
	}}})
	if err != nil {
		t.Fatal(err)
	}
	return store
}

func readGrants(t *testing.T, store relational.Provider, tenant *commonv1.TenantContext) []relational.Row {
	t.Helper()
	db, err := store.Bind(tenant)
	if err != nil {
		t.Fatal(err)
	}
	var rows []relational.Row
	err = db.Transaction(t.Context(), relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		var queryErr error
		rows, queryErr = tx.Query(ctx, "SELECT * FROM sandbox_grants ORDER BY task_id")
		return queryErr
	})
	if err != nil {
		t.Fatal(err)
	}
	return rows
}

func TestTaskBoundUnlimitedGrantPersistsAndRevokes(t *testing.T) {
	store := grantStore(t)
	tenant := &commonv1.TenantContext{TenantId: "t1", WorkspaceId: "w1", SessionId: "ses_01HZY2J5K3M4N5P6Q7R8S9T0VW"}
	now := time.Date(2026, 11, 9, 0, 0, 0, 123456000, time.UTC)
	grant := Grant{TaskID: "task-unlimited", Scope: "public", Minutes: 0, AllowUnlimited: true, ApprovedBy: "user-1", Ports: []int32{80, 443}, Reason: "research", MaxConns: 4}
	if err := WriteSubmitGrant(t.Context(), store, tenant, grant, now); err != nil {
		t.Fatal(err)
	}
	rows := readGrants(t, store, tenant)
	if len(rows) != 1 || rows[0]["expires_at"] != UnlimitedExpiresAt || rows[0]["state"] != "active" || rows[0]["task_id"] != grant.TaskID || rows[0]["scope"] != "public" || rows[0]["max_conns"] != int64(4) {
		t.Fatalf("persisted grant: %+v", rows)
	}
	if len(readGrants(t, store, &commonv1.TenantContext{TenantId: "other"})) != 0 {
		t.Fatal("cross-tenant grant leak")
	}
	// Resubmission never mutates the original grant, including its duration.
	grant.Minutes = 15
	if err := WriteSubmitGrant(t.Context(), store, tenant, grant, now.Add(time.Hour)); err != nil {
		t.Fatal(err)
	}
	rows = readGrants(t, store, tenant)
	if len(rows) != 1 || rows[0]["expires_at"] != UnlimitedExpiresAt {
		t.Fatalf("idempotency changed original grant: %+v", rows)
	}
	if n, err := RevokeTask(t.Context(), store, tenant, "other-task", now); err != nil || n != 0 {
		t.Fatalf("wrong task revoke: %d %v", n, err)
	}
	if n, err := RevokeTask(t.Context(), store, tenant, grant.TaskID, now.Add(time.Hour)); err != nil || n != 1 {
		t.Fatalf("task revoke: %d %v", n, err)
	}
	rows = readGrants(t, store, tenant)
	if rows[0]["state"] != "revoked" || rows[0]["revoked_at"] != ISO(now.Add(time.Hour)) {
		t.Fatalf("not revoked: %+v", rows)
	}
	if n, err := RevokeTask(t.Context(), store, tenant, grant.TaskID, now); err != nil || n != 0 {
		t.Fatalf("revoke not idempotent: %d %v", n, err)
	}
}

func TestFiniteExpiryAndExistingRowsRemainUnchanged(t *testing.T) {
	store := grantStore(t)
	tenant := &commonv1.TenantContext{TenantId: "t1"}
	now := time.Date(2026, 11, 9, 0, 0, 0, 0, time.UTC)
	for _, minutes := range []int32{1, 15, 30, 60} {
		actual, err := grantExpiry(Grant{Minutes: minutes}, now)
		if err != nil || actual != ISO(now.Add(time.Duration(minutes)*time.Minute)) {
			t.Fatalf("minutes=%d expiry=%s err=%v", minutes, actual, err)
		}
	}
	grant := Grant{TaskID: "finite", Minutes: 15, Hosts: []string{"example.com"}, Ports: []int32{443}, ApprovedBy: "u"}
	if err := WriteSubmitGrant(t.Context(), store, tenant, grant, now); err != nil {
		t.Fatal(err)
	}
	grant.Minutes = 0
	grant.AllowUnlimited = true
	if err := WriteSubmitGrant(t.Context(), store, tenant, grant, now.Add(time.Hour)); err != nil {
		t.Fatal(err)
	}
	if got := readGrants(t, store, tenant)[0]["expires_at"]; got != ISO(now.Add(15*time.Minute)) {
		t.Fatalf("existing finite grant was widened: %v", got)
	}
}

func TestInvalidUnlimitedRequestsFailBeforePersistence(t *testing.T) {
	valid := Grant{TaskID: "task", Minutes: 0, ApprovedBy: "user", AllowUnlimited: true}
	for name, g := range map[string]Grant{
		"negative": {Minutes: -1}, "overflow": {Minutes: 1<<31 - 1},
		"unapproved-policy": {TaskID: "task", Minutes: 0, ApprovedBy: "user"},
		"missing-task":      {Minutes: 0, ApprovedBy: "user", AllowUnlimited: true},
		"missing-approver":  {TaskID: "task", Minutes: 0, AllowUnlimited: true},
	} {
		t.Run(name, func(t *testing.T) {
			// A nil provider proves validation precedes any database access.
			err := WriteSubmitGrant(t.Context(), nil, nil, g, time.Now())
			if err == nil {
				t.Fatal("invalid grant persisted")
			}
			if name == "unapproved-policy" && providererr.CategoryOf(err) != providererr.Denied {
				t.Fatalf("want policy denial: %v", err)
			}
		})
	}
	if expires, err := grantExpiry(valid, time.Now()); err != nil || expires != UnlimitedExpiresAt {
		t.Fatalf("valid unlimited rejected: %s %v", expires, err)
	}
}

func TestUnlimitedExpiryRoundTripsUnixSeconds(t *testing.T) {
	parsed, err := time.Parse("2006-01-02T15:04:05.000000Z", UnlimitedExpiresAt)
	if err != nil {
		t.Fatal(err)
	}
	if parsed.Year() != 9999 || parsed.Nanosecond() != 0 {
		t.Fatalf("sentinel must be a whole second within year 9999: %v", parsed)
	}
	if got := ISO(time.Unix(parsed.Unix(), 0)); got != UnlimitedExpiresAt {
		t.Fatalf("Unix seconds changed sentinel: %s", got)
	}
}
