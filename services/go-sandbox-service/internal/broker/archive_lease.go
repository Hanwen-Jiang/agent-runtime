package broker

import (
	"context"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/ledger"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
)

// The operation deadline ends work before its persisted claim can expire. A
// crashed request cannot leave the environment occupied forever.
const archiveOperationTimeout = 30 * time.Minute

func (b *Broker) claimArchive(ctx context.Context, lease *Lease, inspected, renewIdle bool) (context.Context, func(int64) error, error) {
	ctx, cancel := context.WithTimeout(ctx, archiveOperationTimeout)
	if lease.Record == nil {
		return ctx, func(int64) error { cancel(); return nil }, nil
	}
	rec := lease.Record
	if !inspected {
		var err error
		rec, err = b.ledger.Get(ctx, lease.Owner, lease.ID())
		if err != nil {
			cancel()
			return nil, nil, err
		}
		if rec == nil || rec.State != "ready" {
			cancel()
			return nil, nil, providererr.New(providererr.NotFound, "unknown_env", "环境不存在")
		}
	}
	claimed, err := b.ledger.ClaimArchive(ctx, *rec, archiveOperationTimeout+time.Minute)
	if err != nil {
		cancel()
		return nil, nil, err
	}
	lease.Record = &claimed
	return ctx, func(cpuMS int64) error {
		cancel()
		cleanup, done := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
		defer done()
		return b.ledger.FinishArchive(cleanup, claimed, cpuMS, renewIdle)
	}, nil
}

func (b *Broker) releaseArchived(ctx context.Context, lease *Lease, keep bool, cpuMS int64, skipSnapshot bool) (err error) {
	ctx, finish, err := b.claimArchive(ctx, lease, false, true)
	if err != nil {
		return err
	}
	defer func() {
		if e := finish(0); err == nil {
			err = e
		}
	}()
	if lease.Record != nil {
		updated, touchErr := b.ledger.Touch(ctx, *lease.Record, ledger.TouchArgs{CPUMS: cpuMS, ArchiveToken: lease.Record.ArchiveToken})
		if touchErr != nil {
			return touchErr
		}
		lease.Record = &updated
	}
	if keep {
		return nil
	}
	if skipSnapshot || !lease.Archive || !lease.Writable {
		return b.destroy(ctx, lease, false, "")
	}
	if _, err = b.snapshot(ctx, lease); err != nil {
		// An invalid transfer plan or denied storage request does not make
		// unarchived workspace contents disposable. Keep the writer for retry.
		b.log.Warn("工作区归档失败，保留环境等待重试", "workspace", lease.WorkspaceID, "env_ref", lease.ID(), "error", errText(err))
		return err
	}
	return b.destroy(ctx, lease, false, "")
}
