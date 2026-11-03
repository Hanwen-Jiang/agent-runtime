package environmentapi

// ENV-02（Q20–22、Q36–37）：setup、start 的结果是环境表上的事实；ready 不把失败或还在跑的 setup 当作就绪。

import (
	"context"
	"strconv"
	"testing"

	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	envv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/environment/v1"
)

var setupMigration = []relational.Migration{{Version: 207, Name: "environment_setup_fixture", Statements: []string{
	`ALTER TABLE sandbox_envs ADD COLUMN environment_id TEXT NOT NULL DEFAULT ''`,
	`ALTER TABLE sandbox_envs ADD COLUMN setup_state TEXT NOT NULL DEFAULT 'none'`,
	`ALTER TABLE sandbox_envs ADD COLUMN setup_key TEXT NOT NULL DEFAULT ''`,
	`ALTER TABLE sandbox_envs ADD COLUMN setup_finished_at TEXT NOT NULL DEFAULT ''`,
	`ALTER TABLE sandbox_envs ADD COLUMN setup_error TEXT NOT NULL DEFAULT ''`,
	`ALTER TABLE sandbox_envs ADD COLUMN start_state TEXT NOT NULL DEFAULT 'none'`,
}}}

func setFacts(t *testing.T, store relational.Provider, ref string, values map[string]any) {
	t.Helper()
	db, _ := store.Bind(&commonv1.TenantContext{TenantId: "t1"})
	set, args := "", []any{}
	for k, v := range values {
		args = append(args, v)
		if set != "" {
			set += ", "
		}
		set += k + " = $" + strconv.Itoa(len(args))
	}
	args = append(args, ref)
	if err := db.Transaction(context.Background(), relational.TxOptions{}, func(c context.Context, tx relational.Tx) error {
		_, err := tx.Execute(c, "UPDATE sandbox_envs SET "+set+" WHERE env_ref = $"+strconv.Itoa(len(args)), args...)
		return err
	}); err != nil {
		t.Fatal(err)
	}
}

func TestSetupFactsComeFromTheLedgerAndGateReady(t *testing.T) {
	eachStore(t, func(t *testing.T, store relational.Provider) {
		h := newHarness(t, store)
		cases := []struct {
			facts map[string]any
			state envv1.SetupState
			ready bool
		}{
			{map[string]any{}, envv1.SetupState_SETUP_STATE_NONE, true},
			{map[string]any{"environment_id": "env-py", "setup_state": "running", "setup_key": "k1"}, envv1.SetupState_SETUP_STATE_RUNNING, false},
			{map[string]any{"environment_id": "env-py", "setup_state": "succeeded", "setup_key": "k1", "setup_finished_at": "2026-11-03T11:00:00.000000Z", "start_state": "failed"}, envv1.SetupState_SETUP_STATE_SUCCEEDED, true},
			{map[string]any{"environment_id": "env-py", "setup_state": "cached", "setup_key": "k1"}, envv1.SetupState_SETUP_STATE_CACHED, true},
			{map[string]any{"environment_id": "env-py", "setup_state": "failed", "setup_error": "environment_setup_failed"}, envv1.SetupState_SETUP_STATE_FAILED, false},
			{map[string]any{"setup_state": "future-state"}, envv1.SetupState_SETUP_STATE_UNSPECIFIED, false},
		}
		for i, c := range cases {
			ref := "sbe-setup" + strconv.Itoa(i)
			seed(t, store, "t1", ref, nil)
			if i == 0 {
				if err := store.Migrate(ctx, append(append([]relational.Migration{}, testMigration...), setupMigration...)); err != nil {
					t.Fatal(err)
				}
			}
			if len(c.facts) > 0 {
				setFacts(t, store, ref, c.facts)
			}
			r, err := h.client.GetEnvironment(ctx, request(h, "t1", &envv1.GetEnvironmentRequest{EnvRef: ref}))
			if err != nil {
				t.Fatal(err)
			}
			e := r.Msg.Environment
			if e.SetupState != c.state || e.Ready != c.ready {
				t.Fatalf("case %d: setup %v ready %v (%v)", i, e.SetupState, e.Ready, e)
			}
			if i == 2 && (e.EnvironmentId != "env-py" || e.SetupKey != "k1" || e.SetupFinishedAt == nil || e.StartState != envv1.StartState_START_STATE_FAILED) {
				t.Fatalf("facts not projected: %v", e)
			}
			if i == 4 && e.SetupError != "environment_setup_failed" {
				t.Fatalf("error code not projected: %v", e)
			}
		}
	})
}

func TestLedgerWithoutSetupColumnsStillProjects(t *testing.T) {
	eachStore(t, func(t *testing.T, store relational.Provider) {
		h := newHarness(t, store)
		seed(t, store, "t1", "sbe-old", nil)
		r, err := h.client.GetEnvironment(ctx, request(h, "t1", &envv1.GetEnvironmentRequest{EnvRef: "sbe-old"}))
		if err != nil {
			t.Fatal(err)
		}
		// Worker 还没迁移 207：那时还没有 setup，按 NONE；不因缺列而不可用。
		if !r.Msg.Environment.Ready || r.Msg.Environment.SetupState != envv1.SetupState_SETUP_STATE_NONE {
			t.Fatalf("legacy ledger: %v", r.Msg.Environment)
		}
	})
}
