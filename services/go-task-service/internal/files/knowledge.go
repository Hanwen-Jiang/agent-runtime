package files

import (
	"context"
	"encoding/json"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	"strings"
	"time"
	"unicode"
)

type Collection struct {
	ID               string `json:"id"`
	Generation       string `json:"generation"`
	EmbeddingVersion string `json:"embedding_version"`
	Revision         int64  `json:"revision"`
	Roots            []Root `json:"roots"`
}

func knowledgeMigrations() []relational.Migration {
	return []relational.Migration{{Version: 413, Name: "file_collections_and_frozen_generation_slots", Statements: []string{
		`CREATE TABLE runtime_file_collections (collection_id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,created_by TEXT NOT NULL,active_generation TEXT NOT NULL,embedding_version TEXT NOT NULL,roots TEXT NOT NULL,revision BIGINT NOT NULL,updated_at BIGINT NOT NULL)`,
		`CREATE TABLE runtime_file_collection_freezes (task_id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,created_by TEXT NOT NULL,manifest TEXT NOT NULL,created_at BIGINT NOT NULL)`,
		`CREATE TABLE runtime_file_collection_snapshots (task_id TEXT NOT NULL,collection_id TEXT NOT NULL,generation TEXT NOT NULL,embedding_version TEXT NOT NULL,roots TEXT NOT NULL,revision BIGINT NOT NULL,PRIMARY KEY(task_id,collection_id))`,
	}}}
}
func (c *Catalog) PublishCollection(ctx context.Context, t *commonv1.TenantContext, actor string, col Collection, expectedRevision int64) (Collection, error) {
	if len(col.ID) == 0 || len(col.ID) > 128 || len(col.Generation) == 0 || len(col.Generation) > 128 || len(col.EmbeddingVersion) == 0 || len(col.EmbeddingVersion) > 255 || strings.IndexFunc(col.ID+col.Generation+col.EmbeddingVersion, unicode.IsControl) >= 0 || len(col.Roots) < 1 || len(col.Roots) > 500 || expectedRevision < 0 {
		return col, invalid("invalid_collection", "collection identity, generation, embedding and 1–500 roots are required")
	}
	db, e := c.bind(t, actor, t.GetWorkspaceId())
	if e != nil {
		return col, e
	}
	e = db.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		if e := lock(ctx, tx, t.GetWorkspaceId()); e != nil {
			return e
		}
		if e := verifyRoots(ctx, tx, t, actor, col.Roots); e != nil {
			return e
		}
		old, ok, e := tx.QueryOne(ctx, `SELECT created_by,workspace_id,revision,active_generation,embedding_version,roots FROM runtime_file_collections WHERE collection_id=$1`, col.ID)
		if e != nil {
			return e
		}
		if ok && (str(old, "created_by") != actor || str(old, "workspace_id") != t.GetWorkspaceId()) {
			return missing()
		}
		if ok && str(old, "active_generation") == col.Generation {
			if str(old, "embedding_version") != col.EmbeddingVersion || str(old, "roots") != jsonText(col.Roots) {
				return stale()
			}
			col.Revision = num(old, "revision")
			return nil
		}
		if ok && num(old, "revision") != expectedRevision || !ok && expectedRevision != 0 {
			return stale()
		}
		col.Revision = expectedRevision + 1
		if !ok {
			_, e = tx.Execute(ctx, `INSERT INTO runtime_file_collections (collection_id,workspace_id,created_by,active_generation,embedding_version,roots,revision,updated_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)`, col.ID, t.GetWorkspaceId(), actor, col.Generation, col.EmbeddingVersion, jsonText(col.Roots), col.Revision, time.Now().UnixMilli())
		} else {
			var affected int64
			affected, e = tx.Execute(ctx, `UPDATE runtime_file_collections SET active_generation=$1,embedding_version=$2,roots=$3,revision=$4,updated_at=$5 WHERE collection_id=$6 AND revision=$7`, col.Generation, col.EmbeddingVersion, jsonText(col.Roots), col.Revision, time.Now().UnixMilli(), col.ID, expectedRevision)
			if e == nil && affected != 1 {
				return stale()
			}
		}
		return e
	})
	return col, e
}
func (c *Catalog) Collection(ctx context.Context, t *commonv1.TenantContext, actor, id, taskID string) (Collection, error) {
	db, e := c.bind(t, actor, t.GetWorkspaceId())
	if e != nil {
		return Collection{}, e
	}
	var col Collection
	e = db.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		row, ok, e := tx.QueryOne(ctx, `SELECT * FROM runtime_file_collections WHERE collection_id=$1 AND workspace_id=$2 AND created_by=$3`, id, t.GetWorkspaceId(), actor)
		if e != nil {
			return e
		}
		if !ok {
			return missing()
		}
		col = Collection{ID: id, Generation: str(row, "active_generation"), EmbeddingVersion: str(row, "embedding_version"), Revision: num(row, "revision")}
		_ = json.Unmarshal([]byte(str(row, "roots")), &col.Roots)
		if taskID != "" {
			snap, ok, e := tx.QueryOne(ctx, `SELECT g.* FROM runtime_file_collection_snapshots g JOIN runtime_file_snapshots s ON s.snapshot_id=g.task_id WHERE g.task_id=$1 AND g.collection_id=$2 AND s.workspace_id=$3 AND s.created_by=$4`, taskID, id, t.GetWorkspaceId(), actor)
			if e != nil {
				return e
			}
			if !ok {
				return missing()
			}
			col.Generation = str(snap, "generation")
			col.EmbeddingVersion = str(snap, "embedding_version")
			col.Revision = num(snap, "revision")
			_ = json.Unmarshal([]byte(str(snap, "roots")), &col.Roots)
		}
		return verifyRoots(ctx, tx, t, actor, col.Roots)
	})
	return col, e
}
func (c *Catalog) FreezeCollections(ctx context.Context, t *commonv1.TenantContext, actor, task string, roots []Root) ([]Collection, error) {
	db, e := c.bind(t, actor, t.GetWorkspaceId())
	if e != nil {
		return nil, e
	}
	out := []Collection{}
	e = db.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		if e := lock(ctx, tx, t.GetWorkspaceId()); e != nil {
			return e
		}
		snap, exists, e := tx.QueryOne(ctx, `SELECT workspace_id,created_by,manifest FROM runtime_file_snapshots WHERE snapshot_id=$1`, task)
		if e != nil {
			return e
		}
		if !exists || str(snap, "workspace_id") != t.GetWorkspaceId() || str(snap, "created_by") != actor || str(snap, "manifest") != jsonText(roots) {
			return stale()
		}
		if e := verifyRoots(ctx, tx, t, actor, roots); e != nil {
			return e
		}
		frozen, exists, e := tx.QueryOne(ctx, `SELECT workspace_id,created_by,manifest FROM runtime_file_collection_freezes WHERE task_id=$1`, task)
		if e != nil {
			return e
		}
		if exists {
			if str(frozen, "workspace_id") != t.GetWorkspaceId() || str(frozen, "created_by") != actor || json.Unmarshal([]byte(str(frozen, "manifest")), &out) != nil {
				return stale()
			}
			for _, col := range out {
				for _, root := range col.Roots {
					matched := false
					for _, direct := range roots {
						if root.FileID == direct.FileID && root.VersionID == direct.VersionID && root.Epoch == direct.Epoch && root.SHA256 == direct.SHA256 {
							matched = true
							break
						}
					}
					if !matched {
						return stale()
					}
				}
				if e := verifyRoots(ctx, tx, t, actor, col.Roots); e != nil {
					return e
				}
			}
			return nil
		}
		selected := map[string]Root{}
		for _, root := range roots {
			selected[root.VersionID] = root
		}
		rows, e := tx.Query(ctx, `SELECT * FROM runtime_file_collections WHERE workspace_id=$1 AND created_by=$2 ORDER BY collection_id LIMIT 500`, t.GetWorkspaceId(), actor)
		if e != nil {
			return e
		}
		for _, row := range rows {
			col := Collection{ID: str(row, "collection_id"), Generation: str(row, "active_generation"), EmbeddingVersion: str(row, "embedding_version"), Revision: num(row, "revision")}
			if json.Unmarshal([]byte(str(row, "roots")), &col.Roots) != nil {
				return invalid("invalid_collection", "invalid collection roots")
			}
			all := len(col.Roots) > 0
			for _, root := range col.Roots {
				pin, ok := selected[root.VersionID]
				if !ok || pin.FileID != root.FileID || pin.Epoch != root.Epoch || pin.SHA256 != root.SHA256 {
					all = false
				}
			}
			if !all {
				continue
			}
			if e := verifyRoots(ctx, tx, t, actor, col.Roots); e != nil {
				continue
			}
			if _, e = tx.Execute(ctx, `INSERT INTO runtime_file_collection_snapshots (task_id,collection_id,generation,embedding_version,roots,revision) VALUES ($1,$2,$3,$4,$5,$6)`, task, col.ID, col.Generation, col.EmbeddingVersion, jsonText(col.Roots), col.Revision); e != nil {
				return e
			}
			out = append(out, col)
		}
		_, e = tx.Execute(ctx, `INSERT INTO runtime_file_collection_freezes (task_id,workspace_id,created_by,manifest,created_at) VALUES ($1,$2,$3,$4,$5)`, task, t.GetWorkspaceId(), actor, jsonText(out), time.Now().UnixMilli())
		return e
	})
	return out, e
}

// PinSelectedCollections fixes explicit knowledge scope independently from raw
// attachments. Selecting a collection grants retrieval, not arbitrary raw bytes.
func (c *Catalog) PinSelectedCollections(ctx context.Context, t *commonv1.TenantContext, actor, task string, direct []Root, ids []string) ([]Collection, error) {
	if len(ids) > 1 {
		return nil, invalid("invalid_collection_selection", "select at most the current workspace collection")
	}
	db, e := c.bind(t, actor, t.GetWorkspaceId())
	if e != nil {
		return nil, e
	}
	out := []Collection{}
	e = db.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		if e := lock(ctx, tx, t.GetWorkspaceId()); e != nil {
			return e
		}
		if e := verifyRoots(ctx, tx, t, actor, direct); e != nil {
			return e
		}
		old, exists, e := tx.QueryOne(ctx, `SELECT workspace_id,created_by,manifest FROM runtime_file_collection_freezes WHERE task_id=$1`, task)
		if e != nil {
			return e
		}
		if exists {
			if str(old, "workspace_id") != t.GetWorkspaceId() || str(old, "created_by") != actor || json.Unmarshal([]byte(str(old, "manifest")), &out) != nil || len(out) != len(ids) {
				return stale()
			}
			for i, col := range out {
				if col.ID != ids[i] {
					return stale()
				}
				if e := verifyRoots(ctx, tx, t, actor, col.Roots); e != nil {
					return e
				}
			}
		} else {
			for _, id := range ids {
				row, ok, e := tx.QueryOne(ctx, `SELECT * FROM runtime_file_collections WHERE collection_id=$1 AND workspace_id=$2 AND created_by=$3`, id, t.GetWorkspaceId(), actor)
				if e != nil {
					return e
				}
				if !ok {
					return missing()
				}
				col := Collection{ID: id, Generation: str(row, "active_generation"), EmbeddingVersion: str(row, "embedding_version"), Revision: num(row, "revision")}
				if json.Unmarshal([]byte(str(row, "roots")), &col.Roots) != nil {
					return stale()
				}
				if e := verifyRoots(ctx, tx, t, actor, col.Roots); e != nil {
					return e
				}
				out = append(out, col)
			}
		}
		snapshot, ok, e := tx.QueryOne(ctx, `SELECT workspace_id,created_by,manifest FROM runtime_file_snapshots WHERE snapshot_id=$1`, task)
		if e != nil {
			return e
		}
		if ok {
			if str(snapshot, "workspace_id") != t.GetWorkspaceId() || str(snapshot, "created_by") != actor || str(snapshot, "manifest") != jsonText(direct) {
				return stale()
			}
		} else {
			_, e = tx.Execute(ctx, `INSERT INTO runtime_file_snapshots (snapshot_id,workspace_id,created_by,manifest,created_at) VALUES ($1,$2,$3,$4,$5)`, task, t.GetWorkspaceId(), actor, jsonText(direct), time.Now().UnixMilli())
			if e != nil {
				return e
			}
		}
		if exists {
			return nil
		}
		for _, col := range out {
			_, e = tx.Execute(ctx, `INSERT INTO runtime_file_collection_snapshots (task_id,collection_id,generation,embedding_version,roots,revision) VALUES ($1,$2,$3,$4,$5,$6)`, task, col.ID, col.Generation, col.EmbeddingVersion, jsonText(col.Roots), col.Revision)
			if e != nil {
				return e
			}
		}
		_, e = tx.Execute(ctx, `INSERT INTO runtime_file_collection_freezes (task_id,workspace_id,created_by,manifest,created_at) VALUES ($1,$2,$3,$4,$5)`, task, t.GetWorkspaceId(), actor, jsonText(out), time.Now().UnixMilli())
		return e
	})
	return out, e
}
