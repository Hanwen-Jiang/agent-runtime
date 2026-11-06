package server

import (
	"context"
	"encoding/json"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	sandboxv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/sandbox/v1"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/ledger"
)

var transferMigrations = []relational.Migration{{Version: 216, Name: "sandbox_transfers", Statements: []string{
	`CREATE TABLE sandbox_transfers (transfer_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, lease_id TEXT NOT NULL, path TEXT NOT NULL, name TEXT NOT NULL, object_key TEXT NOT NULL, prepared TEXT NOT NULL, expires_at TEXT NOT NULL, result TEXT NOT NULL DEFAULT '')`,
	`CREATE INDEX sandbox_transfers_task ON sandbox_transfers (task_id, expires_at)`,
}}}

func (h *handler) transferTx(ctx context.Context, owner string, readOnly bool, fn func(context.Context, relational.Tx) error) error {
	store, err := h.s.opts.Relational.Bind(&commonv1.TenantContext{TenantId: owner})
	if err != nil {
		return err
	}
	return store.Transaction(ctx, relational.TxOptions{ReadOnly: readOnly}, fn)
}
func (h *handler) loadTransfer(ctx context.Context, owner, id string) (*preparedUpload, error) {
	var p *preparedUpload
	err := h.transferTx(ctx, owner, true, func(ctx context.Context, tx relational.Tx) error {
		row, ok, err := tx.QueryOne(ctx, "SELECT * FROM sandbox_transfers WHERE transfer_id=$1", id)
		if err != nil || !ok {
			return err
		}
		p = &preparedUpload{owner: owner, id: id, task: ledger.Str(row, "task_id"), idem: ledger.Str(row, "idempotency_key"), lease: ledger.Str(row, "lease_id"), path: ledger.Str(row, "path"), name: ledger.Str(row, "name"), key: ledger.Str(row, "object_key"), expires: ledger.ParseISO(ledger.Str(row, "expires_at"))}
		if json.Unmarshal([]byte(ledger.Str(row, "prepared")), &p.prepared) != nil {
			return providererr.New(providererr.Invalid, "transfer_journal_corrupt", "invalid transfer journal")
		}
		if raw := ledger.Str(row, "result"); raw != "" {
			p.result = &sandboxv1.ObjectReference{}
			if json.Unmarshal([]byte(raw), p.result) != nil {
				return providererr.New(providererr.Invalid, "transfer_journal_corrupt", "invalid transfer result")
			}
		}
		return nil
	})
	return p, err
}
func (h *handler) storeTransfer(ctx context.Context, p *preparedUpload) (*preparedUpload, error) {
	raw, _ := json.Marshal(p.prepared)
	err := h.transferTx(ctx, p.owner, false, func(ctx context.Context, tx relational.Tx) error {
		_, err := tx.Execute(ctx, `INSERT INTO sandbox_transfers (transfer_id,task_id,idempotency_key,lease_id,path,name,object_key,prepared,expires_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT (transfer_id) DO NOTHING`, p.id, p.task, p.idem, p.lease, p.path, p.name, p.key, string(raw), ledger.ISO(p.expires))
		return err
	})
	if err != nil {
		return nil, err
	}
	return h.loadTransfer(ctx, p.owner, p.id)
}
func (h *handler) recordTransferResult(ctx context.Context, p *preparedUpload, result *sandboxv1.ObjectReference) error {
	raw, _ := json.Marshal(result)
	return h.transferTx(ctx, p.owner, false, func(ctx context.Context, tx relational.Tx) error {
		_, err := tx.Execute(ctx, "UPDATE sandbox_transfers SET result=$1 WHERE transfer_id=$2", string(raw), p.id)
		return err
	})
}
func preparedResponse(p *preparedUpload) *sandboxv1.PreparedTransfer {
	return &sandboxv1.PreparedTransfer{TransferId: p.id, Sha256: p.prepared.SHA256, SizeBytes: p.prepared.SizeBytes}
}
