package taskapi

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"mime"
	"net/http"
	"strings"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/durable"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/objectstore"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/files"
	"google.golang.org/protobuf/types/known/structpb"
)

func readFileBody(w http.ResponseWriter, r *http.Request, out any) error {
	d := json.NewDecoder(http.MaxBytesReader(w, r.Body, 128<<10))
	d.DisallowUnknownFields()
	if e := d.Decode(out); e != nil {
		return invalid("invalid file request")
	}
	var v any
	if d.Decode(&v) != io.EOF {
		return invalid("invalid file request")
	}
	return nil
}
func (s *Server) fileDetail(w http.ResponseWriter, r *http.Request) {
	id, t, _, e := s.uploadObjects(r)
	if e != nil {
		fileError(w, e)
		return
	}
	v, e := s.files.Detail(r.Context(), t, id.UserID, r.PathValue("file_id"))
	if e != nil {
		fileError(w, e)
		return
	}
	fileJSON(w, 200, publicFileValue(v))
}
func (s *Server) fileTrash(w http.ResponseWriter, r *http.Request) {
	id, t, _, e := s.uploadObjects(r)
	if e != nil {
		fileError(w, e)
		return
	}
	v, e := s.files.Trash(r.Context(), t, id.UserID)
	if e != nil {
		fileError(w, e)
		return
	}
	fileJSON(w, 200, map[string]any{"files": v})
}
func (s *Server) fileMutation(w http.ResponseWriter, r *http.Request) {
	id, t, _, e := s.uploadObjects(r)
	if e != nil {
		fileError(w, e)
		return
	}
	var body struct {
		Action     string `json:"action"`
		Name       string `json:"name"`
		SubjectID  string `json:"subject_id"`
		Permission string `json:"permission"`
	}
	if e = readFileBody(w, r, &body); e != nil {
		fileError(w, e)
		return
	}
	e = s.files.Mutate(r.Context(), t, id.UserID, r.PathValue("file_id"), body.Action, map[string]any{"name": body.Name, "subject_id": body.SubjectID, "permission": body.Permission})
	if e != nil {
		fileError(w, e)
		return
	}
	fileJSON(w, 200, map[string]bool{"ok": true})
}
func (s *Server) filePublish(w http.ResponseWriter, r *http.Request) {
	id, t, _, e := s.uploadObjects(r)
	if e != nil {
		fileError(w, e)
		return
	}
	var body struct {
		ChangeID           string `json:"changeset_id"`
		ExpectedHead       string `json:"expected_head"`
		VerificationDigest string `json:"verification_digest"`
	}
	if e = readFileBody(w, r, &body); e != nil {
		fileError(w, e)
		return
	}
	version, e := s.files.Publish(r.Context(), t, id.UserID, r.PathValue("file_id"), body.ChangeID, body.ExpectedHead, body.VerificationDigest)
	if e != nil {
		fileError(w, e)
		return
	}
	fileJSON(w, 200, map[string]string{"version_id": version})
}
func (s *Server) fileVersionContent(w http.ResponseWriter, r *http.Request) {
	id, t, objects, e := s.uploadObjects(r)
	if e != nil {
		fileError(w, e)
		return
	}
	root, e := s.files.Root(r.Context(), t, id.UserID, r.PathValue("file_id"), r.PathValue("version_id"))
	if e != nil {
		fileError(w, e)
		return
	}
	key, e := objects.KeyOf(root.ObjectURI)
	if e != nil {
		fileError(w, e)
		return
	}
	release, ok := s.acquireUpload(id.TenantID + "/" + id.UserID)
	if !ok {
		fileJSON(w, 429, map[string]string{"error": "download busy"})
		return
	}
	defer release()
	data, e := objects.Get(r.Context(), key)
	if e != nil {
		fileError(w, e)
		return
	}
	sum := sha256.Sum256(data.Data)
	if hex.EncodeToString(sum[:]) != root.SHA256 || data.Info.SHA256 != root.SHA256 || int64(len(data.Data)) != root.SizeBytes {
		fileError(w, invalid("file integrity mismatch"))
		return
	}
	if _, e = s.files.Root(r.Context(), t, id.UserID, root.FileID, root.VersionID); e != nil {
		fileError(w, e)
		return
	}
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.Header().Set("Content-Security-Policy", "sandbox; default-src 'none'")
	w.Header().Set("Content-Type", data.Info.ContentType)
	w.Header().Set("Content-Disposition", mime.FormatMediaType("attachment", map[string]string{"filename": root.Name}))
	w.Write(data.Data)
}
func publicFileValue(value any) any {
	raw, _ := json.Marshal(value)
	var v any
	_ = json.Unmarshal(raw, &v)
	var clean func(any) any
	clean = func(v any) any {
		switch x := v.(type) {
		case []any:
			for i := range x {
				x[i] = clean(x[i])
			}
		case map[string]any:
			for key, y := range x {
				if key == "object_uri" || key == "path" || key == "input_root" || key == "output_dir" || key == "candidate_path" {
					delete(x, key)
				} else {
					x[key] = clean(y)
				}
			}
		}
		return v
	}
	return clean(v)
}
func jobPublic(j files.Job) any { return publicFileValue(j) }

func (s *Server) startFileOperation(ctx context.Context, t *commonv1.TenantContext, user string, j files.Job) error {
	if j.Status != "pending" {
		return nil
	}
	engine, e := s.d.Durable.Bind(t)
	if e != nil {
		return e
	}
	// Only trusted values are sent to the activity. Paths and credentials never
	// come from the browser; the activity materializes the pinned source subset.
	raw, _ := json.Marshal(map[string]any{"job_id": j.ID, "tenant_id": t.GetTenantId(), "user_id": user, "workspace_id": t.GetWorkspaceId(), "operation": j.Operation, "inputs": j.Inputs, "request": j.Request})
	var m map[string]any
	_ = json.Unmarshal(raw, &m)
	in, e := structpb.NewStruct(m)
	if e != nil {
		return e
	}
	_, e = engine.StartWorkflow(ctx, "FileOperationWorkflow", durable.StartOptions{WorkflowID: "fileop-" + j.ID, TaskQueue: "workspace", Input: in, ExecutionTimeout: 20 * time.Minute})
	if e != nil {
		return e
	}
	return s.files.MarkRunning(ctx, t, user, j.ID)
}
func (s *Server) fileStartJob(w http.ResponseWriter, r *http.Request) {
	id, t, _, e := s.uploadObjects(r)
	if e != nil {
		fileError(w, e)
		return
	}
	var body struct {
		Operation string `json:"operation"`
		RequestID string `json:"request_id"`
		Inputs    []struct {
			FileID    string `json:"file_id"`
			VersionID string `json:"version_id"`
		} `json:"inputs"`
		Options  map[string]any `json:"options"`
		Selector map[string]any `json:"selector"`
	}
	if e = readFileBody(w, r, &body); e != nil {
		fileError(w, e)
		return
	}
	inputs := make([]files.Root, len(body.Inputs))
	for i, input := range body.Inputs {
		inputs[i] = files.Root{FileID: input.FileID, VersionID: input.VersionID}
	}
	j, e := s.files.BeginJob(r.Context(), t, id.UserID, body.Operation, body.RequestID, inputs, map[string]any{"options": body.Options, "selector": body.Selector})
	if e != nil {
		fileError(w, e)
		return
	}
	if e = s.startFileOperation(r.Context(), t, id.UserID, j); e != nil {
		fileError(w, e)
		return
	}
	if j.Status == "pending" {
		j.Status = "running"
	}
	fileJSON(w, 202, jobPublic(j))
}
func (s *Server) fileGetJob(w http.ResponseWriter, r *http.Request) {
	id, t, objects, e := s.uploadObjects(r)
	if e != nil {
		fileError(w, e)
		return
	}
	j, e := s.files.Job(r.Context(), t, id.UserID, r.PathValue("job_id"))
	if e != nil {
		fileError(w, e)
		return
	}
	if e = s.startFileOperation(r.Context(), t, id.UserID, j); e != nil {
		fileError(w, e)
		return
	}
	if j.Status == "pending" || j.Status == "running" {
		engine, err := s.d.Durable.Bind(t)
		if err != nil {
			fileError(w, err)
			return
		}
		desc, err := engine.Describe(r.Context(), "fileop-"+j.ID)
		if err != nil {
			fileError(w, err)
			return
		}
		if desc.Status == durable.StatusRunning {
			j.Status = "running"
		} else {
			result := &structpb.Struct{}
			outcome, err := engine.Result(r.Context(), "fileop-"+j.ID, result, 5*time.Second)
			if err != nil {
				fileError(w, err)
				return
			}
			v := result.AsMap()
			if outcome.Status != durable.StatusCompleted {
				v = map[string]any{"status": "failed", "code": "file_operation_failed", "message": "文件处理未完成，可以重试"}
			}
			if e = verifyDerived(r.Context(), objects, t, j.ID, v); e != nil {
				fileError(w, e)
				return
			}
			j, e = s.files.Finish(r.Context(), t, id.UserID, j.ID, v)
			if e != nil {
				fileError(w, e)
				return
			}
		}
	}
	fileJSON(w, 200, jobPublic(j))
}
func (s *Server) fileCancelJob(w http.ResponseWriter, r *http.Request) {
	id, t, _, e := s.uploadObjects(r)
	if e != nil {
		fileError(w, e)
		return
	}
	var body struct {
		ConsumerID string `json:"consumer_id"`
	}
	if e = readFileBody(w, r, &body); e != nil {
		fileError(w, e)
		return
	}
	all, e := s.files.CancelConsumer(r.Context(), t, id.UserID, r.PathValue("job_id"), body.ConsumerID)
	if e != nil {
		fileError(w, e)
		return
	}
	if all {
		engine, err := s.d.Durable.Bind(t)
		e = err
		if err == nil {
			e = engine.Cancel(r.Context(), "fileop-"+r.PathValue("job_id"), "all file consumers cancelled")
		}
		if e != nil {
			fileError(w, e)
			return
		}
	}
	fileJSON(w, 200, map[string]any{"cancelled": true, "job_cancelled": all})
}
func verifyDerived(ctx context.Context, objects objectstore.Store, t *commonv1.TenantContext, job string, result map[string]any) error {
	var total int64
	count := 0
	var visit func(any) error
	visit = func(v any) error {
		switch x := v.(type) {
		case []any:
			for _, y := range x {
				if e := visit(y); e != nil {
					return e
				}
			}
		case map[string]any:
			if raw, ok := x["object_uri"]; ok {
				uri, ok := raw.(string)
				if !ok || !strings.HasPrefix(uri, "obj://"+t.GetTenantId()+"/file-derived/"+job+"/") {
					return invalid("invalid derived reference")
				}
				key, e := objects.KeyOf(uri)
				if e != nil {
					return e
				}
				info, e := objects.Head(ctx, key)
				if e != nil {
					return e
				}
				size, ok := x["size_bytes"].(float64)
				sha, _ := x["sha256"].(string)
				if !ok || int64(size) != info.Size || sha != info.SHA256 {
					return invalid("derived object integrity mismatch")
				}
				total += info.Size
				count++
				if total > 512<<20 || count > 2000 {
					return invalid("derived result quota exceeded")
				}
			}
			for key, y := range x {
				if key != "object_uri" {
					if e := visit(y); e != nil {
						return e
					}
				}
			}
		}
		return nil
	}
	return visit(result)
}
func (s *Server) fileDerivedContent(w http.ResponseWriter, r *http.Request) {
	id, t, objects, e := s.uploadObjects(r)
	if e != nil {
		fileError(w, e)
		return
	}
	j, e := s.files.Job(r.Context(), t, id.UserID, r.PathValue("job_id"))
	if e != nil {
		fileError(w, e)
		return
	}
	wanted := r.PathValue("artifact_id")
	var found map[string]any
	var find func(any)
	find = func(v any) {
		switch x := v.(type) {
		case []any:
			for _, y := range x {
				find(y)
			}
		case map[string]any:
			if x["id"] == wanted && x["object_uri"] != nil {
				found = x
			}
			for _, y := range x {
				find(y)
			}
		}
	}
	find(j.Result)
	if found == nil {
		fileError(w, providererr.New(providererr.NotFound, "artifact_not_found", "artifact not found"))
		return
	}
	uri, _ := found["object_uri"].(string)
	key, e := objects.KeyOf(uri)
	if e != nil {
		fileError(w, e)
		return
	}
	release, ok := s.acquireUpload(id.TenantID + "/" + id.UserID)
	if !ok {
		fileJSON(w, 429, map[string]string{"error": "download busy"})
		return
	}
	defer release()
	info, e := objects.Head(r.Context(), key)
	if e != nil {
		fileError(w, e)
		return
	}
	expectedSize, _ := found["size_bytes"].(float64)
	expectedSHA, _ := found["sha256"].(string)
	if info.Size > maxUploadBytes || info.Size != int64(expectedSize) || info.SHA256 != expectedSHA {
		fileError(w, invalid("derived content mismatch"))
		return
	}
	data, e := objects.Get(r.Context(), key)
	if e != nil {
		fileError(w, e)
		return
	}
	sum := sha256.Sum256(data.Data)
	if hex.EncodeToString(sum[:]) != expectedSHA || int64(len(data.Data)) != info.Size {
		fileError(w, invalid("derived content mismatch"))
		return
	}
	if _, e = s.files.Job(r.Context(), t, id.UserID, j.ID); e != nil {
		fileError(w, e)
		return
	}
	media := data.Info.ContentType
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.Header().Set("Content-Security-Policy", "sandbox; default-src 'none'")
	w.Header().Set("Content-Type", media)
	disposition := "attachment"
	if media == "image/png" || media == "image/jpeg" || media == "application/pdf" {
		disposition = "inline"
	}
	name, _ := found["name"].(string)
	w.Header().Set("Content-Disposition", mime.FormatMediaType(disposition, map[string]string{"filename": name}))
	w.Write(data.Data)
}

func (s *Server) fileAttachJob(w http.ResponseWriter, r *http.Request) {
	id, t, _, e := s.uploadObjects(r)
	if e != nil {
		fileError(w, e)
		return
	}
	var body struct {
		TaskID      string   `json:"task_id"`
		ArtifactIDs []string `json:"artifact_ids"`
	}
	if e = readFileBody(w, r, &body); e != nil {
		fileError(w, e)
		return
	}
	if e = s.files.Attach(r.Context(), t, id.UserID, body.TaskID, r.PathValue("job_id"), body.ArtifactIDs); e != nil {
		fileError(w, e)
		return
	}
	fileJSON(w, 200, map[string]any{"attached": body.ArtifactIDs})
}

// Completion is called by the trusted worker even if every browser disconnects.
// It shares the same integrity and atomic publication checks as status polling.
func (s *Server) fileCompleteJob(w http.ResponseWriter, r *http.Request) {
	if r.Header.Get("X-Ar-File-Token") == "" {
		fileError(w, providererr.New(providererr.Denied, "internal_file_identity_required", "internal file identity required"))
		return
	}
	id, t, objects, e := s.uploadObjects(r)
	if e != nil {
		fileError(w, e)
		return
	}
	var body struct {
		Result map[string]any `json:"result"`
	}
	d := json.NewDecoder(http.MaxBytesReader(w, r.Body, 16<<20))
	d.DisallowUnknownFields()
	if e = d.Decode(&body); e != nil || body.Result == nil {
		fileError(w, invalid("invalid completion result"))
		return
	}
	var extra any
	if d.Decode(&extra) != io.EOF {
		fileError(w, invalid("invalid completion result"))
		return
	}
	if e = verifyDerived(r.Context(), objects, t, r.PathValue("job_id"), body.Result); e != nil {
		fileError(w, e)
		return
	}
	j, e := s.files.Finish(r.Context(), t, id.UserID, r.PathValue("job_id"), body.Result)
	if e != nil {
		fileError(w, e)
		return
	}
	fileJSON(w, 200, jobPublic(j))
}
