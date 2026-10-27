package usage_test

import (
	"context"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/dev/sqlitestore"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	"github.com/agentruntime/agent-runtime/services/go-pkg-mirror/internal/usage"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

// 与 Worker 的迁移 201（agentruntime_worker.sandbox.ledger）里 sandbox_usage 的定义相同；包镜像自己不迁移。
var migration = relational.Migration{Version: 201, Name: "sandbox_usage", Statements: []string{
	`CREATE TABLE sandbox_usage (day TEXT PRIMARY KEY, sandbox_seconds BIGINT NOT NULL DEFAULT 0, egress_bytes BIGINT NOT NULL DEFAULT 0,
	 mirror_miss_bytes BIGINT NOT NULL DEFAULT 0, cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0)`,
}}

func TestCountsAreFlushedPerTenantAndReadBackAfterARestart(t *testing.T) {
	ctx := context.Background()
	dir := t.TempDir()
	store, err := sqlitestore.New(sqlitestore.Options{Dir: dir})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = store.Close() })
	if err := store.Migrate(ctx, []relational.Migration{migration}); err != nil {
		t.Fatal(err)
	}
	now := time.Date(2026, 10, 27, 12, 0, 0, 0, time.UTC)
	tr := usage.New(store)
	tr.Add("t-a", 300, now)
	tr.Add("t-a", 200, now)
	tr.Add("t-b", 7, now)
	tr.Add("_platform", 999, now)
	if got := tr.Used(ctx, "t-a", now); got != 500 {
		t.Fatalf("内存里的数：%d", got)
	}
	if failed := tr.Flush(ctx, now); failed != 0 {
		t.Fatalf("写进业务库：%d 个失败", failed)
	}
	again := usage.New(store) // 换一个进程
	if got := again.Used(ctx, "t-a", now); got != 500 {
		t.Fatalf("从业务库读回：%d", got)
	}
	if got := again.Used(ctx, "t-b", now); got != 7 {
		t.Fatalf("各租户分开：%d", got)
	}
	if got := again.Used(ctx, "t-a", now.Add(24*time.Hour)); got != 0 {
		t.Fatalf("第二天从零算：%d", got)
	}
	b, _ := store.Bind(&commonv1.TenantContext{TenantId: "t-a"})
	_ = b.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		row, ok, err := tx.QueryOne(ctx, "SELECT mirror_miss_bytes, sandbox_seconds FROM sandbox_usage WHERE day = $1", "2026-10-27")
		if err != nil || !ok || row["mirror_miss_bytes"].(int64) != 500 || row["sandbox_seconds"].(int64) != 0 {
			t.Fatalf("业务库里的行：%v %v %v", row, ok, err)
		}
		return nil
	})
	p, _ := store.BindPlatform("check")
	_ = p.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		if _, ok, _ := tx.QueryOne(ctx, "SELECT mirror_miss_bytes FROM sandbox_usage WHERE day = $1", "2026-10-27"); ok {
			t.Fatal("平台作用域不计入")
		}
		return nil
	})
}

func TestUnwritableCountsStayInMemoryUntilTheNextFlush(t *testing.T) {
	ctx := context.Background()
	store, err := sqlitestore.New(sqlitestore.Options{Dir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = store.Close() })
	now := time.Now()
	tr := usage.New(store)
	tr.Add("t-a", 10, now)
	if failed := tr.Flush(ctx, now); failed != 1 {
		t.Fatal("表还没建（Worker 还没迁移）：写不进去")
	}
	if got := tr.Used(ctx, "t-a", now); got != 10 {
		t.Fatalf("数还在：%d", got)
	}
	_ = store.Migrate(ctx, []relational.Migration{migration})
	if failed := tr.Flush(ctx, now); failed != 0 {
		t.Fatal("建好之后补写")
	}
}
