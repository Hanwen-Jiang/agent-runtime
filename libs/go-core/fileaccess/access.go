// Package fileaccess revalidates current catalog authority at the model boundary.
package fileaccess

import (
	"context"
	"encoding/json"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

func denied() error {
	return providererr.New(providererr.Denied, "image_not_authorized", "image access is no longer authorized")
}
func Authorize(ctx context.Context, p relational.Provider, t *commonv1.TenantContext, task, uri, sha string) error {
	if p == nil {
		return denied()
	}
	db, e := p.Bind(t)
	if e != nil {
		return e
	}
	return db.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		row, ok, e := tx.QueryOne(ctx, `SELECT created_by,workspace_id,manifest FROM runtime_file_snapshots WHERE snapshot_id=$1`, task)
		if e != nil {
			return e
		}
		if !ok || row["workspace_id"] != t.GetWorkspaceId() || (t.GetUserId() != "" && row["created_by"] != t.GetUserId()) {
			return denied()
		}
		actor, _ := row["created_by"].(string)
		type root struct {
			FileID    string `json:"file_id"`
			VersionID string `json:"version_id"`
			URI       string `json:"object_uri"`
			SHA       string `json:"sha256"`
			Epoch     int64  `json:"acl_epoch"`
		}
		var pinned []root
		raw, _ := row["manifest"].(string)
		if json.Unmarshal([]byte(raw), &pinned) != nil {
			return denied()
		}
		roots := []root{}
		derived := false
		for _, r := range pinned {
			if r.SHA == sha {
				match := r.URI == uri
				if !match {
					_, ok, e := tx.QueryOne(ctx, `SELECT source_id FROM runtime_file_sources WHERE file_id=$1 AND object_uri=$2`, r.FileID, uri)
					if e != nil {
						return e
					}
					match = ok
				}
				if match {
					roots = append(roots, r)
				}
			}
		}
		if len(roots) == 0 {
			record, ok, e := tx.QueryOne(ctx, `SELECT roots,sha256 FROM runtime_file_task_artifacts WHERE task_id=$1 AND object_uri=$2`, task, uri)
			if e != nil {
				return e
			}
			if !ok || record["sha256"] != sha {
				return denied()
			}
			data, _ := record["roots"].(string)
			if json.Unmarshal([]byte(data), &roots) != nil || len(roots) == 0 {
				return denied()
			}
			derived = true
		}
		for _, r := range roots {
			allowed := false
			for _, pin := range pinned {
				if pin.VersionID == r.VersionID && pin.SHA == r.SHA {
					allowed = true
				}
			}
			if !allowed {
				return denied()
			}
			f, ok, e := tx.QueryOne(ctx, `SELECT f.created_by,f.acl_epoch FROM runtime_files f JOIN runtime_file_versions v ON f.file_id=v.file_id WHERE f.file_id=$1 AND v.version_id=$2 AND v.sha256=$3 AND f.workspace_id=$4 AND f.lifecycle='active'`, r.FileID, r.VersionID, r.SHA, t.GetWorkspaceId())
			if e != nil {
				return e
			}
			if !ok {
				return denied()
			}
			if derived && f["acl_epoch"] != r.Epoch {
				return denied()
			}
			if f["created_by"] != actor {
				_, ok, e = tx.QueryOne(ctx, `SELECT permission FROM runtime_file_grants WHERE file_id=$1 AND workspace_id=$2 AND subject_type='user' AND subject_id=$3`, r.FileID, t.GetWorkspaceId(), actor)
				if e != nil {
					return e
				}
				if !ok {
					return denied()
				}
			}
		}
		return nil
	})
}
