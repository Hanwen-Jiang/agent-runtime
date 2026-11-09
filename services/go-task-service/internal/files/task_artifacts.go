package files

import (
	"context"
	"encoding/json"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

func artifactMigrations() []relational.Migration {
	return []relational.Migration{{Version: 414, Name: "task_derived_image_authorizations", Statements: []string{`CREATE TABLE runtime_file_task_artifacts (task_id TEXT NOT NULL,job_id TEXT NOT NULL,artifact_id TEXT NOT NULL,object_uri TEXT NOT NULL,roots TEXT NOT NULL,sha256 TEXT NOT NULL,mime_type TEXT NOT NULL,size_bytes BIGINT NOT NULL,name TEXT NOT NULL,PRIMARY KEY(task_id,artifact_id))`}}}
}
func (c *Catalog) Attach(ctx context.Context, t *commonv1.TenantContext, actor, taskID, jobID string, ids []string) error {
	if len(ids) == 0 || len(ids) > 10 {
		return invalid("image_limit", "select 1 to 10 rendered images")
	}
	db, e := c.bind(t, actor, t.GetWorkspaceId())
	if e != nil {
		return e
	}
	return db.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		if e := lock(ctx, tx, t.GetWorkspaceId()); e != nil {
			return e
		}
		snap, ok, e := tx.QueryOne(ctx, `SELECT manifest FROM runtime_file_snapshots WHERE snapshot_id=$1 AND workspace_id=$2 AND created_by=$3`, taskID, t.GetWorkspaceId(), actor)
		if e != nil {
			return e
		}
		if !ok {
			return missing()
		}
		var pinned []Root
		if json.Unmarshal([]byte(str(snap, "manifest")), &pinned) != nil {
			return stale()
		}
		row, ok, e := tx.QueryOne(ctx, `SELECT * FROM runtime_file_jobs WHERE job_id=$1 AND workspace_id=$2 AND requested_by=$3 AND status IN ('completed','partial')`, jobID, t.GetWorkspaceId(), actor)
		if e != nil {
			return e
		}
		if !ok {
			return missing()
		}
		j := decodeJob(row)
		if e = verifyRoots(ctx, tx, t, actor, j.Inputs); e != nil {
			return e
		}
		for _, r := range j.Inputs {
			match := false
			for _, p := range pinned {
				if p.VersionID == r.VersionID && p.SHA256 == r.SHA256 {
					match = true
				}
			}
			if !match {
				return missing()
			}
		}
		artifacts, _ := j.Result["artifacts"].([]any)
		for _, id := range ids {
			var a map[string]any
			for _, entry := range artifacts {
				item, _ := entry.(map[string]any)
				if item["id"] == id {
					a = item
					break
				}
			}
			if a == nil {
				return missing()
			}
			media, _ := a["mime_type"].(string)
			size, _ := a["size_bytes"].(float64)
			if (media != "image/png" && media != "image/jpeg" && media != "image/webp") || size <= 0 || size > 10<<20 {
				return invalid("image_limit", "artifact is not an eligible vision image")
			}
			_, e = tx.Execute(ctx, `INSERT INTO runtime_file_task_artifacts (task_id,job_id,artifact_id,object_uri,roots,sha256,mime_type,size_bytes,name) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT DO NOTHING`, taskID, jobID, id, a["object_uri"], jsonText(j.Inputs), a["sha256"], media, int64(size), a["name"])
			if e != nil {
				return e
			}
		}
		quota, _, e := tx.QueryOne(ctx, `SELECT COUNT(*) AS n,CAST(COALESCE(SUM(size_bytes),0) AS BIGINT) AS total FROM runtime_file_task_artifacts WHERE task_id=$1`, taskID)
		if e != nil {
			return e
		}
		if num(quota, "n") > 10 || num(quota, "total") > 20<<20 {
			return invalid("image_limit", "task rendered image quota exceeded")
		}
		return nil
	})
}
