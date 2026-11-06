package registry_test

import (
	"context"
	"github.com/agentruntime/agent-runtime/libs/go-core/personalconfig"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	pb "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/personalconfig/v1"
	"google.golang.org/protobuf/types/known/structpb"
	"sync"
	"testing"
)

func TestPersonalConfigPrivacyImmutablePinsAndNone(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		ctx := context.Background()
		if err := h.store.Migrate(ctx, []relational.Migration{{Version: 214, Name: "personal_test_tasks", Statements: []string{`CREATE TABLE tasks (task_id TEXT PRIMARY KEY, submitted_by TEXT NOT NULL)`}}}); err != nil {
			t.Fatal(err)
		}
		if err := h.store.Migrate(ctx, personalconfig.Migrations()); err != nil {
			t.Fatal(err)
		}
		db, _ := h.store.Bind(alice)
		if err := db.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
			_, err := tx.Execute(ctx, `INSERT INTO tasks(task_id,submitted_by) VALUES ('before','alice'),('pinned','alice')`)
			return err
		}); err != nil {
			t.Fatal(err)
		}
		if err := h.reg.PinPersonalConfig(ctx, alice, "before", "alice"); err != nil {
			t.Fatal(err)
		}
		first, created, err := h.reg.PutPersonalConfig(ctx, alice, &pb.PersonalConfigDocument{Instructions: "first"}, "")
		if err != nil || !created || first.Version != "user/v1" {
			t.Fatalf("first: %v %v %v", first, created, err)
		}
		same, created, err := h.reg.PutPersonalConfig(ctx, alice, first.Document, first.Version)
		if err != nil || created || same.Digest != first.Digest {
			t.Fatalf("same: %v %v %v", same, created, err)
		}
		for _, other := range []*commonv1.TenantContext{bob, eve} {
			if _, err = h.reg.GetPersonalConfig(ctx, other, first.Version); err == nil {
				t.Fatal("private package leaked")
			}
		}
		if err = h.reg.PinPersonalConfig(ctx, alice, "pinned", "bob"); err == nil {
			t.Fatal("caller cannot replace submitted_by")
		}
		if err = h.reg.PinPersonalConfig(ctx, alice, "pinned", "alice"); err != nil {
			t.Fatal(err)
		}
		if _, _, err = h.reg.PutPersonalConfig(ctx, alice, &pb.PersonalConfigDocument{Instructions: "second"}, first.Version); err != nil {
			t.Fatal(err)
		}
		if _, _, err = h.reg.PutPersonalConfig(ctx, alice, first.Document, first.Version); err == nil {
			t.Fatal("stale expected version accepted")
		}
		if _, err = h.reg.RetirePersonalConfig(ctx, alice); err != nil {
			t.Fatal(err)
		}
		if _, err = h.reg.GetPersonalConfig(ctx, alice, ""); err == nil {
			t.Fatal("retired remains current")
		}
		if err = h.reg.PinPersonalConfig(ctx, alice, "pinned", "alice"); err != nil {
			t.Fatal(err)
		}
		got, err := personalconfig.LoadTask(ctx, h.store, &commonv1.TenantContext{TenantId: "t1", UserId: "bob"}, "pinned")
		if err != nil || got.GetDigest() != first.Digest || got.GetDocument().GetInstructions() != "first" {
			t.Fatalf("pinned owner/old version: %v %v", got, err)
		}
		if err = h.reg.PinPersonalConfig(ctx, alice, "before", "alice"); err != nil {
			t.Fatal(err)
		}
		got, err = personalconfig.LoadTask(ctx, h.store, alice, "before")
		if err != nil || got != nil {
			t.Fatalf("absence not pinned: %v %v", got, err)
		}
		got, err = personalconfig.LoadTask(ctx, h.store, eve, "pinned")
		if err != nil || got != nil {
			t.Fatalf("cross tenant: %v %v", got, err)
		}
	})
}
func TestPersonalConfigConcurrentIdenticalPublish(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		var wg sync.WaitGroup
		errors := make(chan error, 6)
		for range 6 {
			wg.Add(1)
			go func() {
				defer wg.Done()
				_, _, err := h.reg.PutPersonalConfig(context.Background(), alice, &pb.PersonalConfigDocument{Instructions: "same"}, "")
				errors <- err
			}()
		}
		wg.Wait()
		close(errors)
		for err := range errors {
			if err != nil {
				t.Fatal(err)
			}
		}
		got, err := h.reg.GetPersonalConfig(context.Background(), alice, "")
		if err != nil || got.GetVersion() != "user/v1" {
			t.Fatalf("%v %v", got, err)
		}
	})
}
func TestPersonalSelectionPinnedAcrossRetries(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		ctx := context.Background()
		if err := h.store.Migrate(ctx, []relational.Migration{{Version: 214, Name: "personal_test_tasks", Statements: []string{`CREATE TABLE tasks (task_id TEXT PRIMARY KEY, submitted_by TEXT NOT NULL)`}}}); err != nil {
			t.Fatal(err)
		}
		if err := h.store.Migrate(ctx, personalconfig.Migrations()); err != nil {
			t.Fatal(err)
		}
		db, _ := h.store.Bind(alice)
		if err := db.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
			_, e := tx.Execute(ctx, `INSERT INTO tasks (task_id,submitted_by,personal_config_pin,personal_config_selection) VALUES ('task','alice','','')`)
			return e
		}); err != nil {
			t.Fatal(err)
		}
		_, _, err := h.reg.PutPersonalConfig(ctx, alice, &pb.PersonalConfigDocument{Defaults: &pb.PersonalConfigDefaults{SkillNames: []string{"first"}, McpConnectionIds: []string{"mc1"}}}, "")
		if err != nil {
			t.Fatal(err)
		}
		if err = h.reg.PinPersonalConfig(ctx, alice, "task", "alice"); err != nil {
			t.Fatal(err)
		}
		override, _ := structpb.NewStruct(map[string]any{"personal_config": map[string]any{"skill_names": []any{"second"}}})
		if err = h.reg.PinPersonalConfig(ctx, alice, "task", "alice", override); err != nil {
			t.Fatal(err)
		}
		selected, err := personalconfig.LoadSelection(ctx, h.store, alice, "task")
		if err != nil || len(selected.SkillNames) != 1 || selected.SkillNames[0] != "first" || selected.MCPConnectionIDs[0] != "mc1" {
			t.Fatalf("pin changed: %v %v", selected, err)
		}
	})
}
