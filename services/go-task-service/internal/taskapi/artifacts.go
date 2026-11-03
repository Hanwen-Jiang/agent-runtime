package taskapi

// N-18 文件交付的公开入口（docs/plan/n18-file-delivery-f3.md，F3）：ListTaskArtifacts 与下载路由
// GET /v1/tasks/{task_id}/artifacts/{artifact_id}。
//
// 元数据在业务库的 task_artifacts（Worker 的 deliver_file 写，这里只读）；表由两边的迁移 209 建，建表语句一致（契约测试比对），
// 谁先启动谁建。内容在 ObjectStore（与 Worker、Claim-Check 同一处）。下载每次都重新核对：令牌里的租户、Task 属于这个租户、
// 文件属于这个 Task、没过期、内容与记下的 sha256 一致。ObjectStore 只有整个读出的 Get，单文件上限 25 MiB 时整个读出再写回。
// 错误不带对象地址；越租户、越 Task 一律按不存在处理。

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"

	"connectrpc.com/connect"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	taskapiv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/taskapi/v1"
)

const (
	routeListArtifacts    = "list_task_artifacts"
	routeDownloadArtifact = "download_task_artifact"
)

// ArtifactsDDL 与 Worker 的迁移 209（agentruntime_worker.tools.deliver.MIGRATIONS）一致；租户列由 RelationalStore 加。
const ArtifactsDDL = `CREATE TABLE task_artifacts (
	artifact_id TEXT PRIMARY KEY,
	task_id TEXT NOT NULL,
	idempotency_key TEXT NOT NULL UNIQUE,
	name TEXT NOT NULL,
	path TEXT NOT NULL,
	media_type TEXT NOT NULL,
	size_bytes BIGINT NOT NULL,
	sha256 TEXT NOT NULL,
	object_uri TEXT NOT NULL,
	created_at TEXT NOT NULL,
	expires_at TEXT NOT NULL
)`

// ArtifactMigrations 返回 task_artifacts 的迁移（209，与 Worker 同一个版本号；已经建过的跳过）。
func ArtifactMigrations() []relational.Migration {
	return []relational.Migration{{Version: 209, Name: "task_artifacts", Statements: []string{ArtifactsDDL}}}
}

var artifactIDPattern = regexp.MustCompile(`^art_[0-7][0-9A-HJKMNP-TV-Z]{25}$`)

// downloadMediaTypes 是下载时原样给出的媒体类型；其余（例如 text/html、image/svg+xml）按 application/octet-stream 给。
var downloadMediaTypes = map[string]bool{
	"text/plain": true, "text/csv": true, "text/markdown": true, "text/tab-separated-values": true,
	"application/json": true, "application/pdf": true, "application/zip": true, "application/gzip": true, "application/x-tar": true,
	"image/png": true, "image/jpeg": true, "image/gif": true, "image/webp": true,
	"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":         true,
	"application/vnd.openxmlformats-officedocument.wordprocessingml.document":   true,
	"application/vnd.openxmlformats-officedocument.presentationml.presentation": true,
}

type artifactRow struct {
	ID, Name, Path, MediaType, SHA256, ObjectURI string
	Size                                         int64
	Created, Expires                             time.Time
}

const artifactColumns = "artifact_id, name, path, media_type, size_bytes, sha256, object_uri, created_at, expires_at"

func decodeArtifact(r relational.Row) (artifactRow, error) {
	str := func(k string) string { v, _ := r[k].(string); return v }
	var size int64
	switch v := r["size_bytes"].(type) {
	case int64:
		size = v
	case int32:
		size = int64(v)
	case int:
		size = int64(v)
	case float64:
		size = int64(v)
	case string:
		n, err := strconv.ParseInt(v, 10, 64)
		if err != nil {
			return artifactRow{}, err
		}
		size = n
	}
	created, err := time.Parse(time.RFC3339Nano, str("created_at"))
	if err != nil {
		return artifactRow{}, err
	}
	expires, err := time.Parse(time.RFC3339Nano, str("expires_at"))
	if err != nil {
		return artifactRow{}, err
	}
	return artifactRow{ID: str("artifact_id"), Name: str("name"), Path: str("path"), MediaType: str("media_type"), SHA256: str("sha256"),
		ObjectURI: str("object_uri"), Size: size, Created: created, Expires: expires}, nil
}

// artifacts 在租户 tenantID 下按 SQL 查 task_artifacts（租户条件由 RelationalStore 加）。
func (s *Server) artifacts(ctx context.Context, tenantID, where string, args ...any) ([]artifactRow, error) {
	store, err := s.d.Store.Bind(&commonv1.TenantContext{TenantId: tenantID})
	if err != nil {
		return nil, err
	}
	var out []artifactRow
	err = store.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		rows, err := tx.Query(ctx, "SELECT "+artifactColumns+" FROM task_artifacts WHERE "+where+" ORDER BY created_at, artifact_id", args...)
		if err != nil {
			return err
		}
		for _, r := range rows {
			a, err := decodeArtifact(r)
			if err != nil {
				return providererr.Wrap(providererr.Invalid, "bad_artifact_row", "task_artifacts 里的一行读不出来", err)
			}
			out = append(out, a)
		}
		return nil
	})
	return out, err
}

// ListTaskArtifacts 列出 Task 交付的文件（只给元数据）。
func (s *Server) ListTaskArtifacts(ctx context.Context, req *connect.Request[taskapiv1.ListTaskArtifactsRequest]) (*connect.Response[taskapiv1.ListTaskArtifactsResponse], error) {
	_, tenant, row, err := s.lookup(ctx, req.Header(), routeListArtifacts, req.Msg.GetTenantId(), req.Msg.GetTaskId())
	if err != nil {
		return nil, ConnectError(err)
	}
	rows, err := s.artifacts(ctx, tenant.GetTenantId(), "task_id = $1", row.TaskID)
	if err != nil {
		return nil, ConnectError(err)
	}
	now := s.d.Clock.Now()
	out := &taskapiv1.ListTaskArtifactsResponse{}
	for _, a := range rows {
		state := taskapiv1.TaskArtifactState_TASK_ARTIFACT_STATE_READY
		if !now.Before(a.Expires) {
			state = taskapiv1.TaskArtifactState_TASK_ARTIFACT_STATE_EXPIRED
		}
		out.Artifacts = append(out.Artifacts, &taskapiv1.TaskArtifact{
			ArtifactId: a.ID, Name: a.Name, Path: a.Path, MediaType: a.MediaType, SizeBytes: a.Size, Sha256: a.SHA256,
			CreatedAt: timestamppb.New(a.Created), ExpiresAt: timestamppb.New(a.Expires), State: state,
			DownloadPath: "/v1/tasks/" + row.TaskID + "/artifacts/" + a.ID,
		})
	}
	return connect.NewResponse(out), nil
}

// downloadStatus 把认证与查找的错误换成 HTTP 状态：没认证 401，限流 429，其余（格式不对、别的租户、不存在）一律 404。
func downloadStatus(err error) int {
	var pe *providererr.Error
	if errors.As(err, &pe) {
		switch {
		case pe.Category == providererr.Denied && pe.Code == "unauthenticated":
			return http.StatusUnauthorized
		case pe.Category == providererr.Transient && pe.Code == "rate_limit":
			return http.StatusTooManyRequests
		case pe.Category == providererr.Transient:
			return http.StatusServiceUnavailable
		}
	}
	return http.StatusNotFound
}

// downloadArtifact 是 GET /v1/tasks/{task_id}/artifacts/{artifact_id}。
func (s *Server) downloadArtifact(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	_, tenant, row, err := s.lookup(ctx, r.Header, routeDownloadArtifact, "", r.PathValue("task_id"))
	if err != nil {
		status := downloadStatus(err)
		http.Error(w, http.StatusText(status), status)
		return
	}
	artifactID := r.PathValue("artifact_id")
	if !artifactIDPattern.MatchString(artifactID) {
		http.Error(w, "文件不存在", http.StatusNotFound)
		return
	}
	if s.d.Objects == nil {
		http.Error(w, "这个部署不能下载交付的文件", http.StatusServiceUnavailable)
		return
	}
	rows, err := s.artifacts(ctx, tenant.GetTenantId(), "task_id = $1 AND artifact_id = $2", row.TaskID, artifactID)
	if err != nil {
		http.Error(w, "暂时读不到文件记录", http.StatusServiceUnavailable)
		return
	}
	if len(rows) != 1 {
		http.Error(w, "文件不存在", http.StatusNotFound)
		return
	}
	a := rows[0]
	if !s.d.Clock.Now().Before(a.Expires) {
		http.Error(w, "文件已过期", http.StatusGone)
		return
	}
	objects, err := s.d.Objects.Bind(&commonv1.TenantContext{TenantId: tenant.GetTenantId()})
	if err != nil {
		http.Error(w, "暂时读不到文件", http.StatusServiceUnavailable)
		return
	}
	key, err := objects.KeyOf(a.ObjectURI)
	if err != nil {
		http.Error(w, "文件记录不对", http.StatusInternalServerError)
		return
	}
	data, err := objects.Get(ctx, key)
	if err != nil {
		if providererr.CategoryOf(err) == providererr.NotFound {
			http.Error(w, "文件已过期", http.StatusGone)
			return
		}
		http.Error(w, "暂时读不到文件", http.StatusServiceUnavailable)
		return
	}
	sum := sha256.Sum256(data.Data)
	if int64(len(data.Data)) != a.Size || hex.EncodeToString(sum[:]) != a.SHA256 {
		http.Error(w, "文件内容与记录不符", http.StatusInternalServerError)
		return
	}
	mediaType := a.MediaType
	if !downloadMediaTypes[mediaType] {
		mediaType = "application/octet-stream"
	}
	h := w.Header()
	h.Set("Content-Type", mediaType)
	h.Set("Content-Length", strconv.Itoa(len(data.Data)))
	h.Set("Content-Disposition", "attachment; filename*=UTF-8''"+rfc5987(a.Name))
	h.Set("X-Content-Type-Options", "nosniff")
	h.Set("Content-Security-Policy", "sandbox")
	h.Set("Cache-Control", "no-store")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(data.Data)
}

// rfc5987 按 RFC 5987 的 attr-char 编码文件名，用在 Content-Disposition 的 filename* 里。
func rfc5987(name string) string {
	var b strings.Builder
	for _, c := range []byte(name) {
		switch {
		case c >= 'A' && c <= 'Z', c >= 'a' && c <= 'z', c >= '0' && c <= '9', strings.IndexByte("!#$&+-.^_`|~", c) >= 0:
			b.WriteByte(c)
		default:
			fmt.Fprintf(&b, "%%%02X", c)
		}
	}
	return b.String()
}
