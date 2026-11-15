package ledger

// ENV-01（SBX-01 从 Worker 的 tests/test_environment_status.py 搬来）：只在实际的健康确认与同会话复借时记下这两件事，
// 普通命令成功不冒充它们；后来探测不健康时清掉确认，不编造状态迁移。别的租户读不到这一行。时间按文本的字典序比较。

import (
	"context"
	"path/filepath"
	"slices"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/dev/sqlitestore"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
)

func TestHealthConfirmationAndReuseAreExplicitLedgerFacts(t *testing.T) {
	store, err := sqlitestore.New(sqlitestore.Options{Dir: filepath.Join(t.TempDir(), "relational")})
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	clock := testkit.NewFakeClock()
	l := New(store, clock)
	ctx := context.Background()
	creating, err := l.BeginCreate(ctx, "t1", CreateArgs{Backend: "test", Kind: "k8s", Isolation: "microvm", WorkspaceID: "w1", SessionID: "ses-a",
		Idle: 5 * time.Minute, MaxLifetime: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	rec, err := l.MarkReady(ctx, creating, "pod-a", "", false)
	if err != nil {
		t.Fatal(err)
	}
	if rec.HealthConfirmedAt != "" || rec.ReusedAt != "" {
		t.Fatal("state=ready 本身不是健康确认")
	}
	if rec, err = l.Touch(ctx, rec, TouchArgs{CPUMS: 12, TaskID: "task-1"}); err != nil || rec.HealthConfirmedAt != "" || rec.ReusedAt != "" {
		t.Fatalf("命令成功不冒充健康与复借：%+v %v", rec, err)
	}
	clock.Advance(time.Second)
	yes, no := true, false
	if rec, err = l.Touch(ctx, rec, TouchArgs{HealthConfirmed: &yes, Reused: true}); err != nil {
		t.Fatal(err)
	}
	now := ISO(clock.Now())
	if rec.HealthConfirmedAt != now || rec.ReusedAt != now || rec.ExpiresAt != ISO(clock.Now().Add(5*time.Minute)) {
		t.Fatalf("rec = %+v", rec)
	}
	if rec, err = l.Touch(ctx, rec, TouchArgs{HealthConfirmed: &no}); err != nil || rec.HealthConfirmedAt != "" || rec.State != "ready" {
		t.Fatalf("后来探测不健康：清掉确认，状态不变：%+v %v", rec, err)
	}
	got, err := l.Get(ctx, "t1", rec.EnvRef)
	if err != nil || got == nil || got.HealthConfirmedAt != rec.HealthConfirmedAt || got.ReusedAt != rec.ReusedAt {
		t.Fatalf("读回来一样：%+v %v", got, err)
	}
	if other, _ := l.Get(ctx, "t2", rec.EnvRef); other != nil {
		t.Fatal("别的租户读不到")
	}
}

func TestISOTimesSortLexically(t *testing.T) {
	base := time.Date(2026, 11, 3, 9, 59, 59, 999_999_000, time.UTC)
	times := []time.Time{base, base.Add(time.Microsecond), base.Add(time.Hour), base.Add(24 * time.Hour)}
	var texts []string
	for _, tm := range times {
		texts = append(texts, ISO(tm))
	}
	if !slices.IsSorted(texts) {
		t.Fatalf("按文本排序就是按时间排序：%v", texts)
	}
	if !ParseISO(texts[1]).Equal(times[1]) {
		t.Fatal("来回不丢精度")
	}
}

func TestArchiveClaimExpiresAndOldCleanupCannotClearReplacement(t *testing.T) {
	store, err := sqlitestore.New(sqlitestore.Options{Dir: filepath.Join(t.TempDir(), "relational")})
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	clock := testkit.NewFakeClock()
	l := New(store, clock)
	ctx := context.Background()
	rec, err := l.BeginCreate(ctx, "t1", CreateArgs{Backend: "test", Kind: "k8s", Isolation: "microvm", WorkspaceID: "w1", Idle: time.Minute, MaxLifetime: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	rec, err = l.MarkReady(ctx, rec, "env-a", "", false)
	if err != nil {
		t.Fatal(err)
	}
	first, err := l.ClaimArchive(ctx, rec, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := l.ClaimArchive(ctx, rec, time.Minute); err == nil {
		t.Fatal("second replica claimed an active archive")
	}
	clock.Advance(time.Minute + time.Second)
	second, err := l.ClaimArchive(ctx, first, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if first.ArchiveToken == second.ArchiveToken {
		t.Fatal("claim needs unique ownership")
	}
	if err := l.FinishArchive(ctx, first, 0, true); err != nil {
		t.Fatal(err)
	}
	current, err := l.Get(ctx, "t1", rec.EnvRef)
	if err != nil || current.ArchiveToken != second.ArchiveToken {
		t.Fatalf("old cleanup cleared replacement: %+v %v", current, err)
	}
	if err := l.FinishArchive(ctx, second, 0, true); err != nil {
		t.Fatal(err)
	}
	current, err = l.Get(ctx, "t1", rec.EnvRef)
	if err != nil || current.ArchiveToken != "" || current.ArchiveUntil != "" {
		t.Fatalf("current owner failed to release: %+v %v", current, err)
	}
}
