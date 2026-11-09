package files

// Finite retention activity. Scheduling belongs to the existing Temporal
// maintenance workflow; this package never creates a timer or background daemon.
import (
	"context"
	"encoding/json"
	"strings"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providers/objectstore"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

type RetentionOptions struct {
	Now    time.Time
	Limit  int
	Prefix string
	Cursor string
}
type RetentionReport struct {
	Queued      int    `json:"queued"`
	Deleted     int    `json:"deleted"`
	Retained    int    `json:"retained"`
	Failed      int    `json:"failed"`
	PurgedFiles int    `json:"purged_files"`
	NextCursor  string `json:"next_cursor"`
	Prefix      string `json:"prefix"`
}

// retainedRoots includes immutable Task snapshots and pending/running jobs.
// Terminal job roots alone are not permanent evidence references. The snapshot
// table is also the hook for future EvidenceRef retention rather than a TTL.
func retainedRoots(ctx context.Context, tx relational.Tx, workspace string) (map[string]bool, error) {
	keep := map[string]bool{}
	rows, e := tx.Query(ctx, `SELECT manifest AS roots FROM runtime_file_snapshots WHERE workspace_id=$1 UNION ALL SELECT inputs AS roots FROM runtime_file_jobs WHERE workspace_id=$1 AND status IN ('pending','running') UNION ALL SELECT roots FROM runtime_file_collections WHERE workspace_id=$1`, workspace)
	if e != nil {
		return nil, e
	}
	for _, row := range rows {
		var roots []Root
		if json.Unmarshal([]byte(str(row, "roots")), &roots) != nil {
			return nil, invalid("invalid_retention_manifest", "cannot collect with invalid retained roots")
		}
		for _, root := range roots {
			keep[root.VersionID] = true
		}
	}
	return keep, nil
}
func queueGC(ctx context.Context, tx relational.Tx, uri, workspace, file, reason string, now int64) error {
	_, e := tx.Execute(ctx, `INSERT INTO runtime_file_gc_objects (object_uri,workspace_id,file_id,reason,state,queued_at,deleted_at) VALUES ($1,$2,$3,$4,'pending',$5,0) ON CONFLICT DO NOTHING`, uri, workspace, file, reason, now)
	return e
}

// CollectGarbage must be invoked by a tenant-authorized maintenance activity,
// never directly from a model tool. Object access remains tenant-bound.
func (c *Catalog) CollectGarbage(ctx context.Context, t *commonv1.TenantContext, objects objectstore.Store, options RetentionOptions) (RetentionReport, error) {
	report := RetentionReport{}
	if t.GetTenantId() == "" || objects == nil {
		return report, invalid("retention_scope_required", "tenant and object store are required")
	}
	if options.Now.IsZero() {
		options.Now = time.Now().UTC()
	}
	if options.Limit <= 0 {
		options.Limit = 100
	}
	if options.Limit > 500 {
		options.Limit = 500
	}
	if options.Prefix == "" {
		options.Prefix = "uploads/"
	}
	if options.Prefix != "uploads/" && options.Prefix != "file-derived/" {
		return report, invalid("retention_prefix", "invalid retention prefix")
	}
	report.Prefix = options.Prefix
	db, e := c.provider.Bind(t)
	if e != nil {
		return report, e
	}
	now := options.Now.UnixMilli()
	e = db.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		expired, e := tx.Query(ctx, `SELECT file_id,workspace_id FROM runtime_files WHERE lifecycle='trash' AND deleted_at <= $1 AND ($3='' OR workspace_id=$3) ORDER BY deleted_at,file_id LIMIT $2`, options.Now.Add(-30*24*time.Hour).UnixMilli(), int64(options.Limit), t.GetWorkspaceId())
		if e != nil {
			return e
		}
		for _, file := range expired {
			workspace, id := str(file, "workspace_id"), str(file, "file_id")
			if e = lock(ctx, tx, workspace); e != nil {
				return e
			}
			current, ok, e := tx.QueryOne(ctx, `SELECT lifecycle,deleted_at FROM runtime_files WHERE file_id=$1`, id)
			if e != nil {
				return e
			}
			if !ok || str(current, "lifecycle") != "trash" || num(current, "deleted_at") > options.Now.Add(-30*24*time.Hour).UnixMilli() {
				continue
			}
			keep, e := retainedRoots(ctx, tx, workspace)
			if e != nil {
				return e
			}
			versions, e := tx.Query(ctx, `SELECT version_id,object_uri FROM runtime_file_versions WHERE file_id=$1`, id)
			if e != nil {
				return e
			}
			retained := false
			for _, version := range versions {
				if keep[str(version, "version_id")] {
					retained = true
					break
				}
			}
			if retained {
				report.Retained++
				continue
			}
			_, e = tx.Execute(ctx, `UPDATE runtime_files SET lifecycle='purging',acl_epoch=acl_epoch+1 WHERE file_id=$1 AND lifecycle='trash'`, id)
			if e != nil {
				return e
			}
			refs, e := tx.Query(ctx, `SELECT object_uri FROM runtime_file_versions WHERE file_id=$1 UNION SELECT object_uri FROM runtime_file_sources WHERE file_id=$1`, id)
			if e != nil {
				return e
			}
			for _, ref := range refs {
				if e = queueGC(ctx, tx, str(ref, "object_uri"), workspace, id, "trash_expired", now); e != nil {
					return e
				}
				report.Queued++
			}
			if e = audit(ctx, tx, id, "retention", "purging", map[string]any{"reason": "trash_expired"}); e != nil {
				return e
			}
		}
		// Audit data contains IDs/actions only. Deletion is bounded just like blobs.
		audits, e := tx.Query(ctx, `SELECT a.audit_id FROM runtime_file_audit a JOIN runtime_files f ON a.file_id=f.file_id WHERE a.created_at < $1 AND ($3='' OR f.workspace_id=$3) ORDER BY a.created_at LIMIT $2`, options.Now.Add(-90*24*time.Hour).UnixMilli(), int64(options.Limit), t.GetWorkspaceId())
		if e != nil {
			return e
		}
		for _, row := range audits {
			if _, e = tx.Execute(ctx, `DELETE FROM runtime_file_audit WHERE audit_id=$1`, str(row, "audit_id")); e != nil {
				return e
			}
		}
		return nil
	})
	if e != nil {
		return report, e
	}
	// Scan one ObjectStore page. Every orphan decision is rechecked while holding
	// the same workspace lock as registration and candidate publication.
	page, e := objects.List(ctx, objectstore.ListOptions{Prefix: options.Prefix, Limit: options.Limit, Cursor: options.Cursor})
	if e != nil {
		return report, e
	}
	report.NextCursor = page.NextCursor
	for _, object := range page.Items {
		if object.CreatedAt.After(options.Now.Add(-24 * time.Hour)) {
			continue
		}
		workspace := object.Metadata["workspace_id"]
		if workspace == "" || (t.GetWorkspaceId() != "" && workspace != t.GetWorkspaceId()) {
			report.Retained++
			continue
		}
		e = db.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
			if e := lock(ctx, tx, workspace); e != nil {
				return e
			}
			_, found, e := tx.QueryOne(ctx, `SELECT object_uri FROM runtime_file_versions WHERE object_uri=$1 UNION SELECT object_uri FROM runtime_file_sources WHERE object_uri=$1`, object.URI)
			if e != nil {
				return e
			}
			if found {
				return nil
			}
			if strings.HasPrefix(object.Key, "file-derived/") {
				bits := strings.Split(object.Key, "/")
				if len(bits) < 3 {
					return nil
				}
				jobID := bits[1]
				job, exists, e := tx.QueryOne(ctx, `SELECT inputs,status,updated_at,workspace_id FROM runtime_file_jobs WHERE job_id=$1`, jobID)
				if e != nil {
					return e
				}
				if exists {
					if str(job, "workspace_id") != workspace || str(job, "status") == "pending" || str(job, "status") == "running" || (str(job, "status") != "expired" && num(job, "updated_at") > options.Now.Add(-30*24*time.Hour).UnixMilli()) {
						report.Retained++
						return nil
					}
					keep, e := retainedRoots(ctx, tx, workspace)
					if e != nil {
						return e
					}
					var roots []Root
					if json.Unmarshal([]byte(str(job, "inputs")), &roots) != nil {
						return invalid("invalid_retention_manifest", "cannot collect invalid job roots")
					}
					for _, root := range roots {
						if keep[root.VersionID] {
							report.Retained++
							return nil
						}
					}
					_, held, e := tx.QueryOne(ctx, `SELECT changeset_id FROM runtime_file_changesets WHERE candidate_uri=$1 AND status IN ('verified','published')`, object.URI)
					if e != nil {
						return e
					}
					if held {
						// Published bytes were retained by the version lookup.
						// Expire an unreviewed candidate with its idle job before deleting it.
						if _, e = tx.Execute(ctx, `UPDATE runtime_file_changesets SET status='expired' WHERE candidate_uri=$1 AND status='verified'`, object.URI); e != nil {
							return e
						}
					}
					// Revoke representation visibility before any bytes are removed.
					if _, e = tx.Execute(ctx, `UPDATE runtime_file_jobs SET status='expired',result='{}',updated_at=$1 WHERE job_id=$2 AND status NOT IN ('pending','running')`, now, jobID); e != nil {
						return e
					}
					if _, e = tx.Execute(ctx, `DELETE FROM runtime_file_representations WHERE job_id=$1`, jobID); e != nil {
						return e
					}
				}
			}
			if e = queueGC(ctx, tx, object.URI, workspace, "", "unreferenced", now); e != nil {
				return e
			}
			report.Queued++
			return nil
		})
		if e != nil {
			return report, e
		}
	}
	var pending []relational.Row
	e = db.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		var err error
		pending, err = tx.Query(ctx, `SELECT object_uri,file_id FROM runtime_file_gc_objects WHERE state='pending' AND ($2='' OR workspace_id=$2) ORDER BY queued_at,object_uri LIMIT $1`, int64(options.Limit), t.GetWorkspaceId())
		return err
	})
	if e != nil {
		return report, e
	}
	for _, row := range pending {
		uri := str(row, "object_uri")
		key, e := objects.KeyOf(uri)
		if e != nil {
			report.Failed++
			continue
		}
		if _, e = objects.Delete(ctx, key); e != nil {
			report.Failed++
			continue
		}
		e = db.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
			if _, e := tx.Execute(ctx, `UPDATE runtime_file_gc_objects SET state='deleted',deleted_at=$1 WHERE object_uri=$2`, now, uri); e != nil {
				return e
			}
			file := str(row, "file_id")
			if file != "" {
				left, _, e := tx.QueryOne(ctx, `SELECT COUNT(*) AS n FROM runtime_file_gc_objects WHERE file_id=$1 AND state='pending'`, file)
				if e != nil {
					return e
				}
				if num(left, "n") == 0 {
					n, e := tx.Execute(ctx, `UPDATE runtime_files SET lifecycle='purged' WHERE file_id=$1 AND lifecycle='purging'`, file)
					if e != nil {
						return e
					}
					report.PurgedFiles += int(n)
				}
			}
			return nil
		})
		if e != nil {
			return report, e
		}
		report.Deleted++
	}
	return report, nil
}
