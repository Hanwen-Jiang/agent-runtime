package taskapi

// User-provided task sources share ObjectStore with the worker. They never
// mutate the workspace or enter the model context without a pinned allowlist.
import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"mime"
	"net/http"
	"path"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/ingress"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/objectstore"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/files"
)

const maxUploadBytes = 50 << 20

var uploadIDPattern = regexp.MustCompile(`^src_[a-f0-9]{32}$`)

func uploadName(value string) string {
	value = path.Base(strings.ReplaceAll(value, `\`, "/"))
	if value == "." || value == ".." || value == "/" || len(value) > 255 || !utf8.ValidString(value) || !printable(value) {
		return ""
	}
	return value
}

func (s *Server) uploadObjects(r *http.Request) (ingress.Identity, *commonv1.TenantContext, objectstore.Store, error) {
	id, err := s.fileIdentity(r)
	if err != nil {
		return id, nil, nil, err
	}
	tenant, err := ingress.TenantContextFor(id, "", r.URL.Query().Get("workspace_id"), "", nil)
	if err != nil {
		return id, nil, nil, err
	}
	if tenant.GetWorkspaceId() == "" {
		return id, tenant, nil, invalid("workspace_id is required")
	}
	if s.d.Objects == nil {
		return id, tenant, nil, providererr.New(providererr.Transient, "uploads_unavailable", "uploads unavailable")
	}
	objects, err := s.d.Objects.Bind(tenant)
	return id, tenant, objects, err
}

func (s *Server) uploadSource(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	id, tenant, objects, err := s.uploadObjects(r)
	if err != nil {
		http.Error(w, "upload unavailable", downloadStatus(err))
		return
	}
	name := uploadName(r.URL.Query().Get("filename"))
	if name == "" {
		http.Error(w, "invalid filename", 400)
		return
	}
	release, ok := s.acquireUpload(id.TenantID + "/" + id.UserID)
	if !ok {
		w.Header().Set("Retry-After", "1")
		fileJSON(w, 429, map[string]string{"error": "upload busy"})
		return
	}
	defer release()
	data, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxUploadBytes))
	if err != nil {
		http.Error(w, "file exceeds 50 MiB", 413)
		return
	}
	media := http.DetectContentType(data)
	sum := sha256.Sum256(data)
	existing, found, err := s.files.FindDuplicate(r.Context(), tenant, id.UserID, tenant.GetWorkspaceId(), name, hex.EncodeToString(sum[:]), int64(len(data)), media)
	if err != nil {
		fileError(w, err)
		return
	}
	if found {
		writeUploadedRecord(w, existing)
		return
	}
	random := make([]byte, 16)
	if _, err = rand.Read(random); err != nil {
		http.Error(w, "upload unavailable", 503)
		return
	}
	uploadID := "src_" + hex.EncodeToString(random)
	info, err := objects.Put(r.Context(), "uploads/"+uploadID+"/"+name, data, objectstore.PutOptions{ContentType: media, IfAbsent: true, Metadata: map[string]string{"filename": name, "uploaded_by": id.UserID, "workspace_id": tenant.GetWorkspaceId()}})
	if err != nil {
		http.Error(w, "upload unavailable", 503)
		return
	}
	item, err := checkedUpload(r.Context(), objects, tenant, id.UserID, info.URI, false)
	if err != nil {
		fileError(w, err)
		return
	}
	record, err := s.files.Register(r.Context(), tenant, id.UserID, item)
	if err != nil {
		// Deterministic rejection leaves no catalog transaction committed. Reclaim
		// this fresh random-key blob; ambiguous/transient commits are reconciled later.
		category := providererr.CategoryOf(err)
		if category == providererr.Invalid || category == providererr.Denied || category == providererr.Conflict {
			_, _ = objects.Delete(r.Context(), info.Key)
		}
		fileError(w, err)
		return
	}
	writeUploadedRecord(w, record)
}

func (s *Server) downloadSource(w http.ResponseWriter, r *http.Request) {
	id, tenant, objects, err := s.uploadObjects(r)
	if err != nil {
		http.Error(w, "source unavailable", downloadStatus(err))
		return
	}
	uploadID, name := r.PathValue("upload_id"), r.PathValue("filename")
	if !uploadIDPattern.MatchString(uploadID) || uploadName(name) != name {
		http.NotFound(w, r)
		return
	}
	key := "uploads/" + uploadID + "/" + name
	info, err := objects.Head(r.Context(), key)
	if err != nil || info.Metadata["uploaded_by"] != id.UserID || info.Metadata["workspace_id"] != tenant.GetWorkspaceId() {
		http.NotFound(w, r)
		return
	}
	item, err := checkedUpload(r.Context(), objects, tenant, id.UserID, info.URI, false)
	if err != nil {
		fileError(w, err)
		return
	}
	release, ok := s.acquireUpload(id.TenantID + "/" + id.UserID)
	if !ok {
		fileJSON(w, 429, map[string]string{"error": "download busy"})
		return
	}
	defer release()
	if info.Size > maxUploadBytes {
		http.Error(w, "source exceeds limit", 413)
		return
	}
	data, err := objects.Get(r.Context(), key)
	if err != nil {
		http.Error(w, "source unavailable", 503)
		return
	}
	sum := sha256.Sum256(data.Data)
	if int64(len(data.Data)) != info.Size || hex.EncodeToString(sum[:]) != info.SHA256 || http.DetectContentType(data.Data) != info.ContentType {
		http.Error(w, "source integrity check failed", 503)
		return
	}
	if _, err = s.files.Register(r.Context(), tenant, id.UserID, item); err != nil {
		fileError(w, err)
		return
	}
	h := w.Header()
	h.Set("Content-Type", http.DetectContentType(data.Data))
	h.Set("Content-Length", strconv.Itoa(len(data.Data)))
	h.Set("Content-Disposition", mime.FormatMediaType("attachment", map[string]string{"filename": name}))
	h.Set("Cache-Control", "no-store")
	h.Set("X-Content-Type-Options", "nosniff")
	h.Set("Content-Security-Policy", "sandbox")
	_, _ = w.Write(data.Data)
}

func (s *Server) validateUploadedSources(ctx context.Context, tenant *commonv1.TenantContext, user string, refs []string) error {
	if len(refs) > 10 {
		return invalid("at most 10 attachments are allowed")
	}
	if len(refs) == 0 {
		return nil
	}
	if s.d.Objects == nil {
		for _, ref := range refs {
			if strings.HasPrefix(ref, "obj://"+tenant.GetTenantId()+"/uploads/src_") {
				return providererr.New(providererr.Transient, "uploads_unavailable", "uploads unavailable")
			}
		}
		return nil
	}
	release, ok := s.acquireUpload(tenant.GetTenantId() + "/" + user)
	if !ok {
		return providererr.New(providererr.Transient, "rate_limit", "file verification busy")
	}
	defer release()
	objects, err := s.d.Objects.Bind(tenant)
	if err != nil {
		return err
	}
	seen := map[string]bool{}
	var total int64
	uploads := make([]files.UploadRecord, 0, len(refs))
	for _, ref := range refs {
		if seen[ref] {
			return invalid("duplicate attachment")
		}
		seen[ref] = true
		record, lookupErr := s.files.ResolveVersion(ctx, tenant, user, ref)
		if lookupErr == nil {
			key, e := objects.KeyOf(record.ObjectURI)
			if e != nil {
				return e
			}
			data, e := objects.Get(ctx, key)
			if e != nil {
				return e
			}
			sum := sha256.Sum256(data.Data)
			if hex.EncodeToString(sum[:]) != record.Version.SHA256 || int64(len(data.Data)) != record.Version.SizeBytes {
				return invalid("source integrity mismatch")
			}
			total += record.Version.SizeBytes
		} else {
			if providererr.CategoryOf(lookupErr) != providererr.NotFound {
				return lookupErr
			}
			item, e := checkedUpload(ctx, objects, tenant, user, ref, true)
			if e != nil {
				return e
			}
			// Known-but-deleted sources cannot be re-adopted: RegisterBatch checks lifecycle.
			uploads = append(uploads, item)
			total += item.SizeBytes
		}
		if total > maxTaskUploadBytes {
			return invalid("task attachments exceed 200 MiB")
		}
	}
	if len(uploads) > 0 {
		_, err = s.files.RegisterBatch(ctx, tenant, user, uploads)
	}
	return err
}

func (s *Server) pinTaskAttachments(ctx context.Context, tenant *commonv1.TenantContext, taskID string, refs []string, create bool) error {
	if s.d.Objects == nil {
		return nil
	} // Older deployments without uploads remain compatible.
	objects, err := s.d.Objects.Bind(tenant)
	if err != nil {
		return err
	}
	if refs == nil {
		refs = []string{}
	}
	roots := []files.Root{}
	for _, ref := range refs {
		record, e := s.files.ResolveVersion(ctx, tenant, tenant.GetUserId(), ref)
		if e != nil {
			return e
		}
		root, e := s.files.Root(ctx, tenant, tenant.GetUserId(), record.File.ID, record.Version.ID)
		if e != nil {
			return e
		}
		roots = append(roots, root)
	}
	if len(roots) > 0 {
		if err = s.files.Snapshot(ctx, tenant, tenant.GetUserId(), taskID, roots); err != nil {
			return err
		}
	}
	collections := []files.Collection{}
	if len(roots) > 0 {
		collections, err = s.files.FreezeCollections(ctx, tenant, tenant.GetUserId(), taskID, roots)
		if err != nil {
			return err
		}
	}
	data, err := json.Marshal(map[string]any{"attachments": refs, "workspace_id": tenant.GetWorkspaceId(), "user_id": tenant.GetUserId(), "versions": roots, "collections": collections})
	if err != nil {
		return err
	}
	key := "task-inputs/" + taskID + "/manifest.json"
	if create {
		_, err = objects.Put(ctx, key, data, objectstore.PutOptions{ContentType: "application/json", IfAbsent: true})
		if err == nil {
			return nil
		}
		if providererr.CategoryOf(err) != providererr.Conflict {
			return err
		}
	}
	old, err := objects.Get(ctx, key)
	if !create && providererr.CategoryOf(err) == providererr.NotFound {
		return nil
	} // Pre-migration completed admission.
	if err != nil {
		return err
	}
	var manifest struct {
		Attachments []string `json:"attachments"`
		WorkspaceID string   `json:"workspace_id"`
	}
	if json.NewDecoder(bytes.NewReader(old.Data)).Decode(&manifest) != nil || !slices.Equal(manifest.Attachments, refs) || (manifest.WorkspaceID != "" && manifest.WorkspaceID != tenant.GetWorkspaceId()) {
		return providererr.New(providererr.Conflict, "attachment_mismatch", "task attachments are already pinned")
	}
	return nil
}
