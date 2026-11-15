package ledger

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
)

// ClaimArchive protects archive/destruction across replicas. The inspected row
// is compared so stale GC cannot claim an environment another call has renewed.
// Archive ownership is separate from Acquire's BusyFor and ordinary Touch.
func (l *Ledger) ClaimArchive(ctx context.Context, rec Record, duration time.Duration) (Record, error) {
	now := l.Now()
	until := ISO(now.Add(duration))
	raw := make([]byte, 16)
	if _, err := rand.Read(raw); err != nil {
		return rec, err
	}
	token := hex.EncodeToString(raw)
	err := l.Tx(ctx, rec.Owner, false, func(ctx context.Context, tx relational.Tx) error {
		n, err := tx.Execute(ctx, "UPDATE sandbox_envs SET archive_token = $1, archive_until = $2 WHERE env_ref = $3 AND state = $4 AND expires_at = $5 AND busy_until = $6 AND (archive_until = '' OR archive_until <= $7)", token, until, rec.EnvRef, rec.State, rec.ExpiresAt, rec.BusyUntil, ISO(now))
		if err != nil {
			return err
		}
		if n != 1 {
			return providererr.New(providererr.Conflict, "workspace_busy", "工作区正在归档或已经被其他操作更新")
		}
		return nil
	})
	if err == nil {
		rec.ArchiveToken = token
		rec.ArchiveUntil = until
	}
	return rec, err
}

// FinishArchive clears only this token, never a replacement claim. Only a ready
// environment gets a fresh idle period; destroyed records retain their times.
func (l *Ledger) FinishArchive(ctx context.Context, rec Record, cpuMS int64, renewIdle bool) error {
	now := l.Now()
	renew := 0
	if renewIdle {
		renew = 1
	}
	expires := minTime(now.Add(time.Duration(rec.IdleSeconds)*time.Second), ParseISO(rec.MaxUntil))
	return l.Tx(ctx, rec.Owner, false, func(ctx context.Context, tx relational.Tx) error {
		_, err := tx.Execute(ctx, "UPDATE sandbox_envs SET archive_token = '', archive_until = '', last_used_at = CASE WHEN state = 'ready' AND $6 = 1 THEN $1 ELSE last_used_at END, expires_at = CASE WHEN state = 'ready' AND $6 = 1 THEN $2 ELSE expires_at END, cpu_ms = cpu_ms + $3 WHERE env_ref = $4 AND archive_token = $5", ISO(now), ISO(expires), cpuMS, rec.EnvRef, rec.ArchiveToken, renew)
		return err
	})
}
