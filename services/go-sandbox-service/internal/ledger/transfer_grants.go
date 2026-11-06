package ledger

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	"time"
)

func (l *Ledger) CreateTransferGrant(ctx context.Context, owner, task, host string) (string, error) {
	id := make([]byte, 16)
	if _, err := rand.Read(id); err != nil {
		return "", err
	}
	key := "sbg-" + hex.EncodeToString(id)
	hosts, _ := json.Marshal([]string{host})
	now := l.Now()
	err := l.Tx(ctx, owner, false, func(ctx context.Context, tx relational.Tx) error {
		_, err := tx.Execute(ctx, "INSERT INTO sandbox_grants (grant_id, task_id, hosts, ports, expires_at, state, approved_by, reason, created_at) VALUES ($1,$2,$3,$4,$5,'active','platform:object-transfer','single object transfer',$6)", key, task, string(hosts), "[443]", ISO(now.Add(5*time.Minute)), ISO(now))
		return err
	})
	return key, err
}
func (l *Ledger) RevokeTransferGrant(ctx context.Context, owner, id string) error {
	return l.Tx(ctx, owner, false, func(ctx context.Context, tx relational.Tx) error {
		_, err := tx.Execute(ctx, "UPDATE sandbox_grants SET state='revoked',revoked_at=$1 WHERE grant_id=$2 AND approved_by='platform:object-transfer'", ISO(l.Now()), id)
		return err
	})
}
