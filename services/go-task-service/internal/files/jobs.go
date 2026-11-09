package files

import (
	"context"
	"encoding/json"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	"math"
	"strings"
	"time"
)

var operations = map[string]bool{"inspect": true, "read": true, "render": true, "archive_list": true, "extract": true, "query": true, "edit": true, "edit_prepare": true, "edit_verify": true, "recalculate": true, "notebook_execute": true, "index": true}

func (c *Catalog) BeginJob(ctx context.Context, t *commonv1.TenantContext, actor, operation, consumer string, inputs []Root, request map[string]any) (Job, error) {
	if !operations[operation] || len(consumer) < 1 || len(consumer) > 128 || strings.ContainsAny(consumer, "\x00\r\n") {
		return Job{}, invalid("invalid_operation", "invalid operation or request_id")
	}
	if len(inputs) == 0 || len(inputs) > 10 {
		return Job{}, invalid("invalid_inputs", "select 1 to 10 files")
	}
	if len(jsonText(request)) > 64<<10 {
		return Job{}, invalid("invalid_request", "operation options exceed 64 KiB")
	}
	db, e := c.bind(t, actor, t.GetWorkspaceId())
	if e != nil {
		return Job{}, e
	}
	var job Job
	e = db.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		if e := lock(ctx, tx, t.GetWorkspaceId()); e != nil {
			return e
		}
		roots := []Root{}
		var total int64
		for index, input := range inputs {
			perm := "read"
			if index == 0 && (operation == "edit" || operation == "recalculate" || operation == "notebook_execute") {
				perm = "write"
			}
			r, e := rootAt(ctx, tx, actor, t.GetWorkspaceId(), input.FileID, input.VersionID, perm)
			if e != nil {
				return e
			}
			roots = append(roots, r)
			total += r.SizeBytes
		}
		if total > 200<<20 {
			return invalid("input_quota", "selected files exceed 200 MiB")
		}
		hash := digest([]any{actor, t.GetWorkspaceId(), operation, roots, request})
		id := "fjob_" + hash[:48]
		old, ok, e := tx.QueryOne(ctx, `SELECT job_id FROM runtime_file_job_consumers WHERE consumer_id=$1 AND requested_by=$2`, consumer, actor)
		if e != nil {
			return e
		}
		if ok {
			existing, found, err := tx.QueryOne(ctx, `SELECT job_id,input_digest FROM runtime_file_jobs WHERE job_id=$1 AND workspace_id=$2 AND requested_by=$3`, str(old, "job_id"), t.GetWorkspaceId(), actor)
			if err != nil {
				return err
			}
			if !found || str(existing, "input_digest") != hash {
				return providererr.New(providererr.Conflict, "request_id_conflict", "request_id belongs to different input")
			}
			id = str(existing, "job_id")
		}
		// A cancelled/failed attempt cannot be revived: its late workflow
		// completion must never publish into a new consumer's request.
		previous, exists, err := tx.QueryOne(ctx, `SELECT status FROM runtime_file_jobs WHERE job_id=$1`, id)
		if err != nil {
			return err
		}
		if !ok && exists && (str(previous, "status") == "failed" || str(previous, "status") == "cancelled" || str(previous, "status") == "expired") {
			id = "fjob_" + digest([]any{hash, consumer})[:48]
		}
		now := time.Now().UnixMilli()
		_, e = tx.Execute(ctx, `INSERT INTO runtime_file_jobs (job_id,workspace_id,requested_by,operation,input_digest,inputs,request,status,result,created_at,updated_at) VALUES ($1,$2,$3,$4,$5,$6,$7,'pending','{}',$8,$8) ON CONFLICT DO NOTHING`, id, t.GetWorkspaceId(), actor, operation, hash, jsonText(roots), jsonText(request), now)
		if e != nil {
			return e
		}
		_, e = tx.Execute(ctx, `INSERT INTO runtime_file_job_consumers (job_id,consumer_id,requested_by,state) VALUES ($1,$2,$3,'active') ON CONFLICT DO NOTHING`, id, consumer, actor)
		if e != nil {
			return e
		}
		bound, found, e := tx.QueryOne(ctx, `SELECT job_id FROM runtime_file_job_consumers WHERE consumer_id=$1 AND requested_by=$2`, consumer, actor)
		if e != nil {
			return e
		}
		if !found || str(bound, "job_id") != id {
			return providererr.New(providererr.Conflict, "request_id_conflict", "request_id belongs to different input")
		}

		row, _, e := tx.QueryOne(ctx, `SELECT * FROM runtime_file_jobs WHERE job_id=$1`, id)
		if e != nil {
			return e
		}
		job = decodeJob(row)
		job.ConsumerID = consumer
		return nil
	})
	return job, e
}
func decodeJob(row relational.Row) Job {
	j := Job{ID: str(row, "job_id"), Operation: str(row, "operation"), Status: str(row, "status"), CreatedAt: num(row, "created_at"), Request: map[string]any{}, Result: map[string]any{}}
	_ = json.Unmarshal([]byte(str(row, "inputs")), &j.Inputs)
	_ = json.Unmarshal([]byte(str(row, "request")), &j.Request)
	_ = json.Unmarshal([]byte(str(row, "result")), &j.Result)
	return j
}
func verifyRoots(ctx context.Context, tx relational.Tx, t *commonv1.TenantContext, actor string, roots []Root) error {
	for _, r := range roots {
		now, e := rootAt(ctx, tx, actor, t.GetWorkspaceId(), r.FileID, r.VersionID, "read")
		if e != nil {
			return e
		}
		if now.Epoch != r.Epoch || now.SHA256 != r.SHA256 || now.ObjectURI != r.ObjectURI || now.SizeBytes != r.SizeBytes {
			return stale()
		}
	}
	return nil
}
func (c *Catalog) Job(ctx context.Context, t *commonv1.TenantContext, actor, id string) (Job, error) {
	db, e := c.bind(t, actor, t.GetWorkspaceId())
	if e != nil {
		return Job{}, e
	}
	var j Job
	e = db.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		row, ok, e := tx.QueryOne(ctx, `SELECT * FROM runtime_file_jobs WHERE job_id=$1 AND workspace_id=$2 AND requested_by=$3`, id, t.GetWorkspaceId(), actor)
		if e != nil {
			return e
		}
		if !ok {
			return missing()
		}
		j = decodeJob(row)
		if e := verifyRoots(ctx, tx, t, actor, j.Inputs); e != nil {
			return e
		}
		_, e = tx.Execute(ctx, `UPDATE runtime_file_jobs SET updated_at=$1 WHERE job_id=$2`, time.Now().UnixMilli(), id)
		return e
	})
	return j, e
}
func (c *Catalog) CancelConsumer(ctx context.Context, t *commonv1.TenantContext, actor, id, consumer string) (bool, error) {
	db, e := c.bind(t, actor, t.GetWorkspaceId())
	if e != nil {
		return false, e
	}
	all := false
	e = db.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		if e := lock(ctx, tx, t.GetWorkspaceId()); e != nil {
			return e
		}
		_, ok, e := tx.QueryOne(ctx, `SELECT job_id FROM runtime_file_jobs WHERE job_id=$1 AND workspace_id=$2 AND requested_by=$3`, id, t.GetWorkspaceId(), actor)
		if e != nil {
			return e
		}
		if !ok {
			return missing()
		}
		n, e := tx.Execute(ctx, `UPDATE runtime_file_job_consumers SET state='cancelled' WHERE job_id=$1 AND consumer_id=$2 AND requested_by=$3`, id, consumer, actor)
		if e != nil {
			return e
		}
		if n == 0 {
			return missing()
		}
		r, _, e := tx.QueryOne(ctx, `SELECT COUNT(*) AS n FROM runtime_file_job_consumers WHERE job_id=$1 AND state='active'`, id)
		if e != nil {
			return e
		}
		all = num(r, "n") == 0
		if all {
			_, e = tx.Execute(ctx, `UPDATE runtime_file_jobs SET status='cancelled',updated_at=$1 WHERE job_id=$2 AND status IN ('pending','running')`, time.Now().UnixMilli(), id)
		}
		return e
	})
	return all, e
}
func (c *Catalog) MarkRunning(ctx context.Context, t *commonv1.TenantContext, actor, id string) error {
	db, e := c.bind(t, actor, t.GetWorkspaceId())
	if e != nil {
		return e
	}
	return db.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		if e := lock(ctx, tx, t.GetWorkspaceId()); e != nil {
			return e
		}
		row, ok, e := tx.QueryOne(ctx, `SELECT * FROM runtime_file_jobs WHERE job_id=$1 AND workspace_id=$2 AND requested_by=$3`, id, t.GetWorkspaceId(), actor)
		if e != nil {
			return e
		}
		if !ok {
			return missing()
		}
		job := decodeJob(row)
		if e := verifyRoots(ctx, tx, t, actor, job.Inputs); e != nil {
			return e
		}
		if job.Status != "pending" && job.Status != "running" {
			return stale()
		}
		_, e = tx.Execute(ctx, `UPDATE runtime_file_jobs SET status='running',updated_at=$1 WHERE job_id=$2 AND workspace_id=$3 AND requested_by=$4 AND status='pending'`, time.Now().UnixMilli(), id, t.GetWorkspaceId(), actor)
		return e
	})
}

// Finish publishes references only after the API verified every returned object.
// Inputs are re-authorized under a write lock, including every root in a join.
func (c *Catalog) Finish(ctx context.Context, t *commonv1.TenantContext, actor, id string, result map[string]any) (Job, error) {
	db, e := c.bind(t, actor, t.GetWorkspaceId())
	if e != nil {
		return Job{}, e
	}
	var j Job
	e = db.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		if e := lock(ctx, tx, t.GetWorkspaceId()); e != nil {
			return e
		}
		row, ok, e := tx.QueryOne(ctx, `SELECT * FROM runtime_file_jobs WHERE job_id=$1 AND workspace_id=$2 AND requested_by=$3`, id, t.GetWorkspaceId(), actor)
		if e != nil {
			return e
		}
		if !ok {
			return missing()
		}
		j = decodeJob(row)
		if e = verifyRoots(ctx, tx, t, actor, j.Inputs); e != nil {
			return e
		}
		if j.Status != "pending" && j.Status != "running" {
			return nil
		}
		consumers, _, e := tx.QueryOne(ctx, `SELECT COUNT(*) AS n FROM runtime_file_job_consumers WHERE job_id=$1 AND state='active'`, id)
		if e != nil {
			return e
		}
		if num(consumers, "n") == 0 {
			return stale()
		}
		status, _ := result["status"].(string)
		if status == "ok" || status == "completed" || status == "ready" {
			status = "completed"
		} else if status == "partial" {
			status = "partial"
		} else {
			status = "failed"
		}
		now := time.Now().UnixMilli()
		if e := checkResultRetention(ctx, tx, result); e != nil {
			return e
		}
		if status == "completed" || status == "partial" {
			rep := "rep_" + digest([]any{id, result})[:40]
			result["representation_id"] = rep
			_, e = tx.Execute(ctx, `INSERT INTO runtime_file_representations (representation_id,job_id,workspace_id,roots,kind,payload,created_at) VALUES ($1,$2,$3,$4,$5,$6,$7) ON CONFLICT DO NOTHING`, rep, id, t.GetWorkspaceId(), jsonText(j.Inputs), j.Operation, jsonText(result), now)
			if e != nil {
				return e
			}
			if candidate, ok := result["candidate"].(map[string]any); ok && len(j.Inputs) > 0 && status == "completed" {
				verification, vok := result["verification"].(map[string]any)
				passed, _ := verification["passed"].(bool)
				sha, _ := candidate["sha256"].(string)
				uri, _ := candidate["object_uri"].(string)
				size, _ := candidate["size_bytes"].(float64)
				media, _ := candidate["mime_type"].(string)
				fidelity, _ := result["required_fidelity"].(string)
				options, _ := j.Request["options"].(map[string]any)
				required, _ := options["fidelity"].(string)
				if required == "" {
					required = "L1"
				}
				candidateSHA, _ := verification["candidate_sha256"].(string)
				baseSHA, _ := verification["base_sha256"].(string)
				criteria, _ := verification["criteria_digest"].(string)
				if (j.Operation != "edit" && j.Operation != "recalculate" && j.Operation != "notebook_execute") || fidelity != required || candidateSHA != sha || baseSHA != j.Inputs[0].SHA256 || !digestPattern.MatchString(criteria) {
					return invalid("unverified_candidate", "verification must bind the frozen base, candidate and fidelity")
				}
				if !vok || !passed || !digestPattern.MatchString(sha) || uri == "" || math.IsNaN(size) || math.IsInf(size, 0) || math.Trunc(size) != size || size < 0 || size > float64(c.limits.MaxFileBytes) || (fidelity != "L0" && fidelity != "L1" && fidelity != "L2") {
					return invalid("unverified_candidate", "candidate needs passing verification and explicit fidelity")
				}
				r := j.Inputs[0]
				cid := "chg_" + digest([]any{id, sha, verification})[:40]
				result["changeset_id"] = cid
				result["verification_digest"] = digest(verification)
				_, e = tx.Execute(ctx, `INSERT INTO runtime_file_changesets (changeset_id,job_id,file_id,expected_head,acl_epoch,candidate_uri,candidate_sha256,candidate_size,mime_type,required_fidelity,verification_digest,status,created_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,'verified',$12) ON CONFLICT DO NOTHING`, cid, id, r.FileID, r.VersionID, r.Epoch, uri, sha, int64(size), media, fidelity, digest(verification), now)
				if e != nil {
					return e
				}
			}
		}
		_, e = tx.Execute(ctx, `UPDATE runtime_file_jobs SET status=$1,result=$2,updated_at=$3 WHERE job_id=$4`, status, jsonText(result), now, id)
		if e != nil {
			return e
		}
		j.Status = status
		j.Result = result
		return nil
	})
	return j, e
}
func (c *Catalog) Publish(ctx context.Context, t *commonv1.TenantContext, actor, fileID, changeID, expectedHead, verificationDigest string) (string, error) {
	db, e := c.bind(t, actor, t.GetWorkspaceId())
	if e != nil {
		return "", e
	}
	var version string
	e = db.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		if e := lock(ctx, tx, t.GetWorkspaceId()); e != nil {
			return e
		}
		f, e := authorize(ctx, tx, actor, t.GetWorkspaceId(), fileID, "write", false)
		if e != nil {
			return e
		}
		ch, ok, e := tx.QueryOne(ctx, `SELECT * FROM runtime_file_changesets WHERE changeset_id=$1 AND file_id=$2`, changeID, fileID)
		if e != nil {
			return e
		}
		if !ok {
			return missing()
		}
		if verificationDigest != str(ch, "verification_digest") || expectedHead != str(ch, "expected_head") {
			return stale()
		}
		jobRow, jobFound, e := tx.QueryOne(ctx, `SELECT * FROM runtime_file_jobs WHERE job_id=$1 AND workspace_id=$2`, str(ch, "job_id"), t.GetWorkspaceId())
		if e != nil {
			return e
		}
		if !jobFound {
			return missing()
		}
		job := decodeJob(jobRow)
		if e = verifyRoots(ctx, tx, t, actor, job.Inputs); e != nil {
			return e
		}
		if str(ch, "status") == "published" {
			version = str(ch, "published_version")
			return nil
		}
		if str(ch, "status") != "verified" || str(f, "head_version_id") != expectedHead || num(f, "acl_epoch") != num(ch, "acl_epoch") {
			return stale()
		}
		if _, queued, e := tx.QueryOne(ctx, `SELECT object_uri FROM runtime_file_gc_objects WHERE object_uri=$1`, str(ch, "candidate_uri")); e != nil {
			return e
		} else if queued {
			return stale()
		}
		quota, _, e := tx.QueryOne(ctx, `SELECT CAST(COALESCE(SUM(v.size_bytes),0) AS BIGINT) AS n FROM runtime_file_versions v JOIN runtime_files f ON f.file_id=v.file_id WHERE f.workspace_id=$1 AND f.lifecycle IN ('active','trash')`, t.GetWorkspaceId())
		if e != nil {
			return e
		}
		if num(quota, "n")+num(ch, "candidate_size") > c.limits.MaxWorkspaceBytes {
			return providererr.New(providererr.Denied, "file_quota_exceeded", "retained versions exceed workspace quota")
		}
		version = "ver_" + digest([]any{fileID, changeID, str(ch, "candidate_sha256")})[:32]
		now := time.Now().UnixMilli()
		_, e = tx.Execute(ctx, `INSERT INTO runtime_file_versions (version_id,file_id,object_uri,mime_type,size_bytes,sha256,created_at,created_by,parent_version_id) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)`, version, fileID, str(ch, "candidate_uri"), str(ch, "mime_type"), num(ch, "candidate_size"), str(ch, "candidate_sha256"), now, actor, expectedHead)
		if e != nil {
			return e
		}
		n, e := tx.Execute(ctx, `UPDATE runtime_files SET head_version_id=$1 WHERE file_id=$2 AND head_version_id=$3 AND acl_epoch=$4 AND lifecycle='active'`, version, fileID, expectedHead, num(ch, "acl_epoch"))
		if e != nil {
			return e
		}
		if n != 1 {
			return stale()
		}
		_, e = tx.Execute(ctx, `UPDATE runtime_file_changesets SET status='published',published_version=$1 WHERE changeset_id=$2`, version, changeID)
		if e != nil {
			return e
		}
		return audit(ctx, tx, fileID, actor, "publish", map[string]string{"changeset_id": changeID, "version_id": version, "verification_digest": verificationDigest})
	})
	return version, e
}

func checkResultRetention(ctx context.Context, tx relational.Tx, value any) error {
	switch typed := value.(type) {
	case map[string]any:
		if uri, ok := typed["object_uri"].(string); ok {
			if _, queued, e := tx.QueryOne(ctx, `SELECT object_uri FROM runtime_file_gc_objects WHERE object_uri=$1`, uri); e != nil {
				return e
			} else if queued {
				return stale()
			}
		}
		for _, child := range typed {
			if e := checkResultRetention(ctx, tx, child); e != nil {
				return e
			}
		}
	case []any:
		for _, child := range typed {
			if e := checkResultRetention(ctx, tx, child); e != nil {
				return e
			}
		}
	}
	return nil
}
