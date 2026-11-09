package files

// The catalog is the authority for original AND derived bytes. Every publication
// takes the same workspace write lock as revocation; stale jobs cannot republish.
import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"strings"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

func lifecycleMigrations() []relational.Migration {
	return []relational.Migration{{Version: 411, Name: "file_intelligence_lifecycle", Statements: []string{
		`ALTER TABLE runtime_files ADD COLUMN acl_epoch BIGINT NOT NULL DEFAULT 1`,
		`ALTER TABLE runtime_files ADD COLUMN deleted_at BIGINT NOT NULL DEFAULT 0`,
		`CREATE TABLE runtime_file_grants (file_id TEXT NOT NULL, workspace_id TEXT NOT NULL, subject_type TEXT NOT NULL, subject_id TEXT NOT NULL, permission TEXT NOT NULL, created_at BIGINT NOT NULL, PRIMARY KEY(file_id,workspace_id,subject_type,subject_id))`,
		`CREATE TABLE runtime_file_jobs (job_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, requested_by TEXT NOT NULL, operation TEXT NOT NULL, input_digest TEXT NOT NULL, inputs TEXT NOT NULL, request TEXT NOT NULL, status TEXT NOT NULL, result TEXT NOT NULL, created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL)`,
		`CREATE TABLE runtime_file_job_consumers (job_id TEXT NOT NULL, consumer_id TEXT NOT NULL, requested_by TEXT NOT NULL, state TEXT NOT NULL, PRIMARY KEY(job_id,consumer_id))`,
		`CREATE UNIQUE INDEX runtime_file_consumer_request ON runtime_file_job_consumers (requested_by,consumer_id)`,
		`CREATE TABLE runtime_file_representations (representation_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, workspace_id TEXT NOT NULL, roots TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, created_at BIGINT NOT NULL)`,
		`CREATE TABLE runtime_file_changesets (changeset_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, file_id TEXT NOT NULL, expected_head TEXT NOT NULL, acl_epoch BIGINT NOT NULL, candidate_uri TEXT NOT NULL, candidate_sha256 TEXT NOT NULL, candidate_size BIGINT NOT NULL, mime_type TEXT NOT NULL, required_fidelity TEXT NOT NULL, verification_digest TEXT NOT NULL, status TEXT NOT NULL, published_version TEXT NOT NULL DEFAULT '', created_at BIGINT NOT NULL)`,
		`CREATE TABLE runtime_file_audit (audit_id TEXT PRIMARY KEY, file_id TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, details TEXT NOT NULL, created_at BIGINT NOT NULL)`,
		`CREATE TABLE runtime_file_gc_objects (object_uri TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, file_id TEXT NOT NULL, reason TEXT NOT NULL, state TEXT NOT NULL, queued_at BIGINT NOT NULL, deleted_at BIGINT NOT NULL)`,
		`CREATE TABLE runtime_file_snapshots (snapshot_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, created_by TEXT NOT NULL, manifest TEXT NOT NULL, created_at BIGINT NOT NULL)`,
	}}}
}

type Root struct {
	FileID    string `json:"file_id"`
	VersionID string `json:"version_id"`
	Epoch     int64  `json:"acl_epoch"`
	SHA256    string `json:"sha256"`
	ObjectURI string `json:"object_uri,omitempty"`
	Name      string `json:"name"`
	SizeBytes int64  `json:"size_bytes"`
}
type Grant struct {
	SubjectID   string `json:"subject_id"`
	Permission  string `json:"permission"`
	WorkspaceID string `json:"workspace_id"`
}
type Job struct {
	ID         string         `json:"id"`
	Operation  string         `json:"operation"`
	Status     string         `json:"status"`
	ConsumerID string         `json:"consumer_id,omitempty"`
	Inputs     []Root         `json:"inputs"`
	Request    map[string]any `json:"request"`
	Result     map[string]any `json:"result"`
	CreatedAt  int64          `json:"created_at"`
}

func str(r relational.Row, k string) string { s, _ := r[k].(string); return s }
func num(r relational.Row, k string) int64  { n, _ := r[k].(int64); return n }
func digest(v any) string {
	b, _ := json.Marshal(v)
	h := sha256.Sum256(b)
	return hex.EncodeToString(h[:])
}
func jsonText(v any) string { b, _ := json.Marshal(v); return string(b) }
func lock(ctx context.Context, tx relational.Tx, workspace string) error {
	_, e := tx.Execute(ctx, `INSERT INTO runtime_file_catalog_locks (workspace_id,revision) VALUES ($1,0) ON CONFLICT DO NOTHING`, workspace)
	if e != nil {
		return e
	}
	_, e = tx.Execute(ctx, `UPDATE runtime_file_catalog_locks SET revision=revision+1 WHERE workspace_id=$1`, workspace)
	return e
}
func authorize(ctx context.Context, tx relational.Tx, actor, workspace, fileID, permission string, trash bool) (relational.Row, error) {
	row, ok, e := tx.QueryOne(ctx, `SELECT * FROM runtime_files WHERE file_id=$1 AND workspace_id=$2`, fileID, workspace)
	if e != nil {
		return nil, e
	}
	if !ok || (!trash && str(row, "lifecycle") != "active") {
		return nil, missing()
	}
	if str(row, "created_by") == actor {
		return row, nil
	}
	if permission == "owner" || trash {
		return nil, missing()
	}
	g, ok, e := tx.QueryOne(ctx, `SELECT permission FROM runtime_file_grants WHERE file_id=$1 AND workspace_id=$2 AND subject_type='user' AND subject_id=$3`, fileID, workspace, actor)
	if e != nil {
		return nil, e
	}
	if !ok || (permission == "write" && str(g, "permission") != "write") {
		return nil, missing()
	}
	return row, nil
}
func rootAt(ctx context.Context, tx relational.Tx, actor, workspace, fileID, versionID, permission string) (Root, error) {
	f, e := authorize(ctx, tx, actor, workspace, fileID, permission, false)
	if e != nil {
		return Root{}, e
	}
	if versionID == "" {
		versionID = str(f, "head_version_id")
	}
	v, ok, e := tx.QueryOne(ctx, `SELECT * FROM runtime_file_versions WHERE version_id=$1 AND file_id=$2`, versionID, fileID)
	if e != nil {
		return Root{}, e
	}
	if !ok {
		return Root{}, missing()
	}
	return Root{FileID: fileID, VersionID: versionID, Epoch: num(f, "acl_epoch"), SHA256: str(v, "sha256"), ObjectURI: str(v, "object_uri"), Name: str(f, "name"), SizeBytes: num(v, "size_bytes")}, nil
}
func (c *Catalog) Root(ctx context.Context, t *commonv1.TenantContext, actor, fileID, versionID string) (Root, error) {
	db, e := c.bind(t, actor, t.GetWorkspaceId())
	if e != nil {
		return Root{}, e
	}
	var r Root
	e = db.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		var e error
		r, e = rootAt(ctx, tx, actor, t.GetWorkspaceId(), fileID, versionID, "read")
		return e
	})
	return r, e
}
func audit(ctx context.Context, tx relational.Tx, file, actor, action string, details any) error {
	now := time.Now().UnixMilli()
	_, e := tx.Execute(ctx, `INSERT INTO runtime_file_audit (audit_id,file_id,actor,action,details,created_at) VALUES ($1,$2,$3,$4,$5,$6)`, "aud_"+digest([]any{file, actor, action, details, time.Now().UnixNano()})[:32], file, actor, action, jsonText(details), now)
	return e
}

// Mutate supports explicit user grants within the authenticated project workspace.
// Moving to trash revokes grants before any asynchronous cache/blob cleanup.
func (c *Catalog) Mutate(ctx context.Context, t *commonv1.TenantContext, actor, fileID, action string, args map[string]any) error {
	db, e := c.bind(t, actor, t.GetWorkspaceId())
	if e != nil {
		return e
	}
	return db.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		if e := lock(ctx, tx, t.GetWorkspaceId()); e != nil {
			return e
		}
		f, e := authorize(ctx, tx, actor, t.GetWorkspaceId(), fileID, "owner", true)
		if e != nil {
			return e
		}
		switch action {
		case "trash":
			if str(f, "lifecycle") != "active" && str(f, "lifecycle") != "trash" {
				return missing()
			}
			if str(f, "lifecycle") == "trash" {
				return nil
			}
			if _, e = tx.Execute(ctx, `DELETE FROM runtime_file_grants WHERE file_id=$1`, fileID); e != nil {
				return e
			}
			_, e = tx.Execute(ctx, `UPDATE runtime_files SET lifecycle='trash',deleted_at=$1,acl_epoch=acl_epoch+1 WHERE file_id=$2`, time.Now().UnixMilli(), fileID)
		case "restore":
			if str(f, "lifecycle") != "trash" {
				return invalid("not_in_trash", "file is not in trash")
			}
			if time.Now().UnixMilli()-num(f, "deleted_at") > int64(30*24*time.Hour/time.Millisecond) {
				return invalid("restore_expired", "restore window expired")
			}
			_, e = tx.Execute(ctx, `UPDATE runtime_files SET lifecycle='active',deleted_at=0,acl_epoch=acl_epoch+1 WHERE file_id=$1`, fileID)
		case "rename":
			if str(f, "lifecycle") != "active" && str(f, "lifecycle") != "trash" {
				return missing()
			}
			name, _ := args["name"].(string)
			if !validName(name) {
				return invalid("invalid_name", "invalid file name")
			}
			_, e = tx.Execute(ctx, `UPDATE runtime_files SET name=$1 WHERE file_id=$2`, name, fileID)
		case "grant", "revoke":
			if str(f, "lifecycle") != "active" {
				return missing()
			}
			subject, _ := args["subject_id"].(string)
			permission, _ := args["permission"].(string)
			if len(subject) < 1 || len(subject) > 128 || strings.ContainsAny(subject, "\x00\r\n") {
				return invalid("invalid_subject", "a user identity is required")
			}
			if action == "grant" && permission != "read" && permission != "write" {
				return invalid("invalid_permission", "permission must be read or write")
			}
			if _, e = tx.Execute(ctx, `DELETE FROM runtime_file_grants WHERE file_id=$1 AND workspace_id=$2 AND subject_type='user' AND subject_id=$3`, fileID, t.GetWorkspaceId(), subject); e != nil {
				return e
			}
			if action == "grant" {
				_, e = tx.Execute(ctx, `INSERT INTO runtime_file_grants (file_id,workspace_id,subject_type,subject_id,permission,created_at) VALUES ($1,$2,'user',$3,$4,$5)`, fileID, t.GetWorkspaceId(), subject, permission, time.Now().UnixMilli())
				if e != nil {
					return e
				}
			}
			_, e = tx.Execute(ctx, `UPDATE runtime_files SET acl_epoch=acl_epoch+1 WHERE file_id=$1`, fileID)
		default:
			return invalid("invalid_action", "unsupported file action")
		}
		if e != nil {
			return e
		}
		if action == "trash" {
			jobs, e := tx.Query(ctx, `SELECT job_id,inputs FROM runtime_file_jobs WHERE workspace_id=$1 AND status IN ('pending','running')`, t.GetWorkspaceId())
			if e != nil {
				return e
			}
			for _, job := range jobs {
				var roots []Root
				if json.Unmarshal([]byte(str(job, "inputs")), &roots) != nil {
					return invalid("invalid_job_roots", "cannot revoke invalid job roots")
				}
				for _, root := range roots {
					if root.FileID == fileID {
						if _, e = tx.Execute(ctx, `UPDATE runtime_file_jobs SET status='cancelled',updated_at=$1 WHERE job_id=$2`, time.Now().UnixMilli(), str(job, "job_id")); e != nil {
							return e
						}
						if _, e = tx.Execute(ctx, `UPDATE runtime_file_job_consumers SET state='cancelled' WHERE job_id=$1`, str(job, "job_id")); e != nil {
							return e
						}
						break
					}
				}
			}
		}
		return audit(ctx, tx, fileID, actor, action, args)
	})
}
func (c *Catalog) Detail(ctx context.Context, t *commonv1.TenantContext, actor, fileID string) (map[string]any, error) {
	db, e := c.bind(t, actor, t.GetWorkspaceId())
	if e != nil {
		return nil, e
	}
	out := map[string]any{}
	e = db.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		f, e := authorize(ctx, tx, actor, t.GetWorkspaceId(), fileID, "read", false)
		if e != nil {
			return e
		}
		out["file"] = f
		owner := str(f, "created_by") == actor
		canWrite := owner
		if !owner {
			grant, found, e := tx.QueryOne(ctx, `SELECT permission FROM runtime_file_grants WHERE file_id=$1 AND workspace_id=$2 AND subject_type='user' AND subject_id=$3`, fileID, t.GetWorkspaceId(), actor)
			if e != nil {
				return e
			}
			canWrite = found && str(grant, "permission") == "write"
		}
		out["permissions"] = map[string]bool{"read": true, "write": canWrite, "manage": owner}
		versions, e := tx.Query(ctx, `SELECT version_id,file_id,mime_type,size_bytes,sha256,created_at,created_by,parent_version_id FROM runtime_file_versions WHERE file_id=$1 ORDER BY created_at DESC,version_id DESC`, fileID)
		if e != nil {
			return e
		}
		out["versions"] = versions
		grants := []relational.Row{}
		if str(f, "created_by") == actor {
			grants, e = tx.Query(ctx, `SELECT subject_type,subject_id,permission,workspace_id FROM runtime_file_grants WHERE file_id=$1`, fileID)
			if e != nil {
				return e
			}
		}
		out["grants"] = grants
		jobs, e := tx.Query(ctx, `SELECT job_id,operation,status,inputs,result,created_at FROM runtime_file_jobs WHERE workspace_id=$1 AND requested_by=$2 ORDER BY created_at DESC LIMIT 100`, t.GetWorkspaceId(), actor)
		if e != nil {
			return e
		}
		filtered := []Job{}
		for _, j := range jobs {
			var inputs []Root
			_ = json.Unmarshal([]byte(str(j, "inputs")), &inputs)
			for _, root := range inputs {
				if root.FileID == fileID {
					if e := verifyRoots(ctx, tx, t, actor, inputs); e != nil {
						break
					}
					result := map[string]any{}
					_ = json.Unmarshal([]byte(str(j, "result")), &result)
					filtered = append(filtered, Job{ID: str(j, "job_id"), Operation: str(j, "operation"), Status: str(j, "status"), Result: result, CreatedAt: num(j, "created_at")})
					break
				}
			}
		}
		out["jobs"] = filtered
		changes, e := tx.Query(ctx, `SELECT changeset_id,job_id,expected_head,candidate_sha256,candidate_size,required_fidelity,verification_digest,status,published_version,created_at FROM runtime_file_changesets WHERE file_id=$1 ORDER BY created_at DESC`, fileID)
		if e != nil {
			return e
		}
		out["changesets"] = changes
		return nil
	})
	return out, e
}
func (c *Catalog) Trash(ctx context.Context, t *commonv1.TenantContext, actor string) ([]relational.Row, error) {
	db, e := c.bind(t, actor, t.GetWorkspaceId())
	if e != nil {
		return nil, e
	}
	var out []relational.Row
	e = db.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		var e error
		out, e = tx.Query(ctx, `SELECT file_id,name,deleted_at,head_version_id FROM runtime_files WHERE workspace_id=$1 AND created_by=$2 AND lifecycle='trash' ORDER BY deleted_at DESC LIMIT 500`, t.GetWorkspaceId(), actor)
		return e
	})
	return out, e
}
func (c *Catalog) Versions(ctx context.Context, t *commonv1.TenantContext, actor string, ids []Root) ([]Root, error) {
	if len(ids) == 0 || len(ids) > 10 {
		return nil, invalid("invalid_inputs", "select 1 to 10 files")
	}
	db, e := c.bind(t, actor, t.GetWorkspaceId())
	if e != nil {
		return nil, e
	}
	out := []Root{}
	e = db.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		var total int64
		for _, r := range ids {
			v, e := rootAt(ctx, tx, actor, t.GetWorkspaceId(), r.FileID, r.VersionID, "read")
			if e != nil {
				return e
			}
			total += v.SizeBytes
			if total > 200<<20 {
				return invalid("input_quota", "selected files exceed 200 MiB")
			}
			out = append(out, v)
		}
		return nil
	})
	return out, e
}
func stale() error {
	return providererr.New(providererr.Conflict, "file_snapshot_stale", "source version, permission or lifecycle changed; start a new operation")
}

// ResolveVersion resolves immutable content, never a mutable logical head.
func (c *Catalog) ResolveVersion(ctx context.Context, t *commonv1.TenantContext, actor, uri string) (Record, error) {
	db, e := c.bind(t, actor, t.GetWorkspaceId())
	if e != nil {
		return Record{}, e
	}
	var out Record
	e = db.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		row, ok, e := tx.QueryOne(ctx, `SELECT `+recordColumns+` FROM runtime_files f JOIN runtime_file_versions v ON f.file_id=v.file_id WHERE f.workspace_id=$1 AND (v.object_uri=$2 OR (v.parent_version_id='' AND EXISTS(SELECT 1 FROM runtime_file_sources s WHERE s.file_id=f.file_id AND s.object_uri=$2)))`, t.GetWorkspaceId(), uri)
		if e != nil {
			return e
		}
		if !ok {
			return missing()
		}
		if _, e = authorize(ctx, tx, actor, t.GetWorkspaceId(), str(row, "file_id"), "read", false); e != nil {
			return e
		}
		out = decode(row)
		return nil
	})
	return out, e
}
func (c *Catalog) Snapshot(ctx context.Context, t *commonv1.TenantContext, actor, task string, roots []Root) error {
	db, e := c.bind(t, actor, t.GetWorkspaceId())
	if e != nil {
		return e
	}
	return db.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		if e := lock(ctx, tx, t.GetWorkspaceId()); e != nil {
			return e
		}
		if e := verifyRoots(ctx, tx, t, actor, roots); e != nil {
			return e
		}
		existing, ok, e := tx.QueryOne(ctx, `SELECT workspace_id,created_by,manifest FROM runtime_file_snapshots WHERE snapshot_id=$1`, task)
		if e != nil {
			return e
		}
		if ok {
			if str(existing, "workspace_id") != t.GetWorkspaceId() || str(existing, "created_by") != actor || str(existing, "manifest") != jsonText(roots) {
				return stale()
			}
			return nil
		}
		_, e = tx.Execute(ctx, `INSERT INTO runtime_file_snapshots (snapshot_id,workspace_id,created_by,manifest,created_at) VALUES ($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING`, task, t.GetWorkspaceId(), actor, jsonText(roots), time.Now().UnixMilli())
		return e
	})
}
