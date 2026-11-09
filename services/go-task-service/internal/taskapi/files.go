package taskapi

import (
	"connectrpc.com/connect"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strconv"
	"strings"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/objectstore"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/files"
)

const maxTaskUploadBytes = 200 << 20

func fileJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
func fileError(w http.ResponseWriter, err error) {
	status := downloadStatus(err)
	code := "file_unavailable"
	message := "file unavailable"
	var ce *connect.Error
	if errors.As(err, &ce) {
		code = "knowledge_" + ce.Code().String()
		message = "retrieval service unavailable"
		switch ce.Code() {
		case connect.CodeUnavailable, connect.CodeDeadlineExceeded:
			status = 503
		case connect.CodeResourceExhausted:
			status = 429
		case connect.CodeInvalidArgument:
			status = 400
			message = "invalid retrieval request"
		case connect.CodeUnauthenticated:
			status = 401
		case connect.CodePermissionDenied:
			status = 403
		case connect.CodeNotFound:
			status = 404
			message = "retrieval source unavailable"
		case connect.CodeAlreadyExists, connect.CodeAborted, connect.CodeFailedPrecondition:
			status = 409
			message = "retrieval generation conflict"
		default:
			status = 503
		}
	}
	var pe *providererr.Error
	if errors.As(err, &pe) {
		code = pe.Code
		switch pe.Category {
		case providererr.Invalid:
			status = 400
			message = pe.Message
		case providererr.Conflict:
			status = 409
			message = "file registration conflict"
		}
		if pe.Code == "file_too_large" || pe.Code == "file_quota_exceeded" {
			status = 413
			message = "file quota exceeded"
		}
	}
	fileJSON(w, status, map[string]string{"error": message, "code": code})
}
func (s *Server) fileCapabilities(w http.ResponseWriter, r *http.Request) {
	_, _, _, err := s.uploadObjects(r)
	if err != nil {
		fileError(w, err)
		return
	}
	fileJSON(w, 200, map[string]any{"max_file_bytes": maxUploadBytes, "max_task_files": 10, "max_task_bytes": maxTaskUploadBytes, "max_workspace_files": 500, "max_workspace_bytes": int64(2 << 30)})
}
func (s *Server) listFiles(w http.ResponseWriter, r *http.Request) {
	id, tenant, _, err := s.uploadObjects(r)
	if err != nil {
		fileError(w, err)
		return
	}
	limit := 50
	if raw := r.URL.Query().Get("limit"); raw != "" {
		limit, err = strconv.Atoi(raw)
		if err != nil || limit < 1 || limit > 100 {
			fileError(w, invalid("limit must be between 1 and 100"))
			return
		}
	}
	page, err := s.files.List(r.Context(), tenant, id.UserID, tenant.GetWorkspaceId(), limit, r.URL.Query().Get("cursor"))
	if err != nil {
		fileError(w, err)
		return
	}
	fileJSON(w, 200, page)
}
func (s *Server) getFile(w http.ResponseWriter, r *http.Request) {
	id, tenant, _, err := s.uploadObjects(r)
	if err != nil {
		fileError(w, err)
		return
	}
	record, err := s.files.GetBySourceID(r.Context(), tenant, id.UserID, tenant.GetWorkspaceId(), r.PathValue("file_id"))
	if err != nil {
		fileError(w, err)
		return
	}
	fileJSON(w, 200, record)
}
func checkedUpload(ctx context.Context, objects objectstore.Store, tenant *commonv1.TenantContext, owner, uri string, verifyBytes bool) (files.UploadRecord, error) {
	key, err := objects.KeyOf(uri)
	if err != nil {
		return files.UploadRecord{}, missing("source not found")
	}
	parts := strings.Split(key, "/")
	if len(parts) != 3 || parts[0] != "uploads" || !uploadIDPattern.MatchString(parts[1]) || uploadName(parts[2]) != parts[2] {
		return files.UploadRecord{}, missing("source not found")
	}
	info, err := objects.Head(ctx, key)
	if err != nil {
		return files.UploadRecord{}, err
	}
	if info.Metadata["uploaded_by"] != owner || info.Metadata["workspace_id"] != tenant.GetWorkspaceId() || info.Metadata["filename"] != parts[2] {
		return files.UploadRecord{}, missing("source not found")
	}
	if info.Size < 0 || info.Size > maxUploadBytes {
		return files.UploadRecord{}, providererr.New(providererr.Invalid, "file_too_large", "file exceeds configured size")
	}
	if verifyBytes {
		data, err := objects.Get(ctx, key)
		if err != nil {
			return files.UploadRecord{}, err
		}
		sum := sha256.Sum256(data.Data)
		if int64(len(data.Data)) != info.Size || hex.EncodeToString(sum[:]) != info.SHA256 || http.DetectContentType(data.Data) != info.ContentType {
			return files.UploadRecord{}, providererr.New(providererr.Conflict, "file_integrity_mismatch", "file metadata does not match source bytes")
		}
	}
	return files.UploadRecord{SourceID: parts[1], WorkspaceID: tenant.GetWorkspaceId(), Name: parts[2], ObjectURI: uri, MIMEType: info.ContentType, SizeBytes: info.Size, SHA256: info.SHA256, CreatedAt: info.CreatedAt}, nil
}
func (s *Server) adoptFiles(w http.ResponseWriter, r *http.Request) {
	id, tenant, objects, err := s.uploadObjects(r)
	if err != nil {
		fileError(w, err)
		return
	}
	var body struct {
		ObjectURIs []string `json:"object_uris"`
	}
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20))
	decoder.DisallowUnknownFields()
	if err = decoder.Decode(&body); err != nil || len(body.ObjectURIs) == 0 || len(body.ObjectURIs) > 500 {
		fileError(w, invalid("object_uris must contain 1 to 500 source references"))
		return
	}
	var extra any
	if decoder.Decode(&extra) != io.EOF {
		fileError(w, invalid("invalid adoption payload"))
		return
	}
	release, ok := s.acquireUpload(id.TenantID + "/" + id.UserID)
	if !ok {
		w.Header().Set("Retry-After", "1")
		fileJSON(w, 429, map[string]string{"error": "file processing busy"})
		return
	}
	defer release()
	uploads := make([]files.UploadRecord, 0, len(body.ObjectURIs))
	for _, uri := range body.ObjectURIs {
		item, err := checkedUpload(r.Context(), objects, tenant, id.UserID, uri, true)
		if err != nil {
			fileError(w, err)
			return
		}
		uploads = append(uploads, item)
	}
	records, err := s.files.RegisterBatch(r.Context(), tenant, id.UserID, uploads)
	if err != nil {
		fileError(w, err)
		return
	}
	fileJSON(w, 200, files.Page{Files: records, NextCursor: ""})
}

// The existing ObjectStore interface accepts []byte. Two full-file slots bound
// the logical payload to 100 MiB per instance; they are not a heap/RSS promise.
func (s *Server) acquireUpload(owner string) (func(), bool) {
	s.uploadMu.Lock()
	defer s.uploadMu.Unlock()
	if s.uploadTotal >= 2 || s.uploadOwners[owner] >= 2 {
		return nil, false
	}
	s.uploadTotal++
	s.uploadOwners[owner]++
	return func() {
		s.uploadMu.Lock()
		defer s.uploadMu.Unlock()
		s.uploadTotal--
		s.uploadOwners[owner]--
		if s.uploadOwners[owner] == 0 {
			delete(s.uploadOwners, owner)
		}
	}, true
}

func writeUploadedRecord(w http.ResponseWriter, record files.Record) {
	fileJSON(w, http.StatusCreated, map[string]any{"id": record.File.ID, "file_id": record.File.ID, "version_id": record.Version.ID, "uri": record.ObjectURI, "name": record.File.Name, "mime_type": record.Version.MIMEType, "size_bytes": record.Version.SizeBytes, "sha256": record.Version.SHA256, "created_at": record.File.CreatedAt})
}
