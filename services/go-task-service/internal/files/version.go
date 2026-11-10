package files

import (
	"context"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

// Version returns one explicitly selected immutable version after current ACL
// checks. It never substitutes the logical head or accepts another file's ID.
func (c *Catalog) Version(ctx context.Context, t *commonv1.TenantContext, actor, fileID, versionID string) (Record, error) {
	db, e := c.bind(t, actor, t.GetWorkspaceId())
	if e != nil {
		return Record{}, e
	}
	var result Record
	e = db.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		if _, e := authorize(ctx, tx, actor, t.GetWorkspaceId(), fileID, "read", false); e != nil {
			return e
		}
		row, ok, e := tx.QueryOne(ctx, `SELECT `+recordColumns+` FROM runtime_files f JOIN runtime_file_versions v ON f.file_id=v.file_id WHERE f.file_id=$1 AND v.version_id=$2 AND f.workspace_id=$3`, fileID, versionID, t.GetWorkspaceId())
		if e != nil {
			return e
		}
		if !ok {
			return missing()
		}
		result = decode(row)
		return nil
	})
	return result, e
}
