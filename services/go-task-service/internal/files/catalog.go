package files

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"mime"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

var sourceIDPattern = regexp.MustCompile(`^src_[a-f0-9]{32}$`)
var digestPattern = regexp.MustCompile(`^[a-f0-9]{64}$`)

// Migrations uses the shared migration registry. Version 410 was checked against
// both Go and Python migrations. The provider supplies tenant isolation; no
// application SQL or schema defines a tenant discriminator.
func Migrations() []relational.Migration {
	return []relational.Migration{{Version: 410, Name: "runtime_file_catalog_initial_versions", Statements: []string{
		`CREATE TABLE runtime_files (
			file_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, name TEXT NOT NULL,
			created_by TEXT NOT NULL, created_at BIGINT NOT NULL,
			head_version_id TEXT NOT NULL, lifecycle TEXT NOT NULL
		)`,
		`CREATE TABLE runtime_file_versions (
			version_id TEXT PRIMARY KEY, file_id TEXT NOT NULL, object_uri TEXT NOT NULL UNIQUE,
			mime_type TEXT NOT NULL, size_bytes BIGINT NOT NULL, sha256 TEXT NOT NULL,
			created_at BIGINT NOT NULL, created_by TEXT NOT NULL, parent_version_id TEXT NOT NULL
		)`,
		`CREATE TABLE runtime_file_sources (
			source_id TEXT PRIMARY KEY, file_id TEXT NOT NULL,
			object_uri TEXT NOT NULL UNIQUE, created_at BIGINT NOT NULL
		)`,
		`CREATE TABLE runtime_file_catalog_locks (workspace_id TEXT PRIMARY KEY, revision BIGINT NOT NULL)`,
	}}}
}

type Catalog struct {
	provider relational.Provider
	limits   Limits
}

func New(provider relational.Provider, options ...Options) *Catalog {
	catalog := &Catalog{provider: provider, limits: DefaultLimits()}
	if len(options) > 0 {
		catalog.limits = options[0].Limits
	}
	return catalog
}

func invalid(code, message string) error { return providererr.New(providererr.Invalid, code, message) }
func missing() error {
	return providererr.New(providererr.NotFound, "file_not_found", "file not found")
}
func conflict() error {
	return providererr.New(providererr.Conflict, "file_registration_conflict", "source is already registered with different metadata")
}

func (c *Catalog) bind(tenant *commonv1.TenantContext, owner, workspace string) (relational.Store, error) {
	if tenant.GetTenantId() == "" || owner == "" || workspace == "" {
		return nil, invalid("file_scope_required", "tenant, owner and workspace are required")
	}
	if tenant.GetUserId() != "" && tenant.GetUserId() != owner || tenant.GetWorkspaceId() != "" && tenant.GetWorkspaceId() != workspace {
		return nil, missing()
	}
	return c.provider.Bind(tenant)
}

func validName(name string) bool {
	return name != "" && name != "." && name != ".." && len(name) <= 255 && utf8.ValidString(name) &&
		!strings.ContainsAny(name, `/\`) && strings.IndexFunc(name, unicode.IsControl) < 0
}

func validateUpload(tenant *commonv1.TenantContext, upload UploadRecord) error {
	if !sourceIDPattern.MatchString(upload.SourceID) || !digestPattern.MatchString(upload.SHA256) || !validName(upload.Name) || upload.SizeBytes < 0 {
		return invalid("invalid_file_metadata", "invalid file identity, name, digest or size")
	}
	if upload.CreatedAt.IsZero() || upload.CreatedAt.Year() < 1970 || upload.CreatedAt.Year() > 9999 {
		return invalid("invalid_file_timestamp", "file creation time is required")
	}
	if _, _, err := mime.ParseMediaType(upload.MIMEType); err != nil || len(upload.MIMEType) > 255 || strings.IndexFunc(upload.MIMEType, unicode.IsControl) >= 0 {
		return invalid("invalid_file_mime", "invalid file media type")
	}
	if upload.ObjectURI != "obj://"+tenant.GetTenantId()+"/uploads/"+upload.SourceID+"/"+upload.Name {
		return invalid("invalid_file_reference", "file reference does not match its tenant, source identity and name")
	}
	return nil
}

// InitialVersionID is stable across processes and upload/adoption retries.
func InitialVersionID(sourceID, digest string) string {
	sum := sha256.Sum256([]byte(sourceID + digest))
	return "ver_" + hex.EncodeToString(sum[:])[:32]
}

func initialRecord(owner string, upload UploadRecord) Record {
	created := upload.CreatedAt.UTC().Truncate(time.Millisecond)
	versionID := InitialVersionID(upload.SourceID, upload.SHA256)
	return Record{
		File: File{ID: upload.SourceID, WorkspaceID: upload.WorkspaceID, Name: upload.Name, CreatedBy: owner,
			CreatedAt: created, HeadVersionID: versionID, Lifecycle: "active"},
		Version: FileVersion{ID: versionID, FileID: upload.SourceID, MIMEType: upload.MIMEType, SizeBytes: upload.SizeBytes,
			SHA256: upload.SHA256, CreatedAt: created, CreatedBy: owner},
		ObjectURI: upload.ObjectURI,
	}
}

const recordColumns = `f.file_id, f.workspace_id, f.name, f.created_by, f.created_at, f.head_version_id, f.lifecycle,
	v.version_id, v.object_uri, v.mime_type, v.size_bytes, v.sha256, v.parent_version_id`
const recordFrom = ` FROM runtime_files f JOIN runtime_file_versions v ON f.head_version_id = v.version_id AND f.file_id = v.file_id`

func decode(row relational.Row) Record {
	str := func(key string) string { value, _ := row[key].(string); return value }
	num := func(key string) int64 { value, _ := row[key].(int64); return value }
	created := time.UnixMilli(num("created_at")).UTC()
	return Record{
		File: File{ID: str("file_id"), WorkspaceID: str("workspace_id"), Name: str("name"), CreatedBy: str("created_by"),
			CreatedAt: created, HeadVersionID: str("head_version_id"), Lifecycle: str("lifecycle")},
		Version: FileVersion{ID: str("version_id"), FileID: str("file_id"), MIMEType: str("mime_type"), SizeBytes: num("size_bytes"),
			SHA256: str("sha256"), CreatedAt: created, CreatedBy: str("created_by"), ParentVersionID: str("parent_version_id")},
		ObjectURI: str("object_uri"),
	}
}

// Register publishes a single upload through the same atomic path as adoption.
func (c *Catalog) Register(ctx context.Context, tenant *commonv1.TenantContext, owner string, upload UploadRecord) (Record, error) {
	records, err := c.RegisterBatch(ctx, tenant, owner, []UploadRecord{upload})
	if err != nil {
		return Record{}, err
	}
	return records[0], nil
}

const sourceFrom = recordFrom + ` JOIN runtime_file_sources s ON s.file_id = f.file_id`
const sourceColumns = recordColumns + `, s.source_id AS source_id, s.object_uri AS source_object_uri, s.created_at AS source_created_at`

// RegisterBatch adopts 1..500 verified uploads from one workspace atomically.
// The workspace lock serializes deduplication and quota checks across replicas.
// Results correspond to the input order; repeated bytes under different source
// IDs retain every original reference as an alias of one canonical FileVersion.
// A failed validation, conflict or quota check publishes none of the batch.
func (c *Catalog) RegisterBatch(ctx context.Context, tenant *commonv1.TenantContext, owner string, uploads []UploadRecord) ([]Record, error) {
	if len(uploads) < 1 || len(uploads) > 500 {
		return nil, invalid("invalid_file_batch", "file registration requires 1 to 500 sources")
	}
	workspace := uploads[0].WorkspaceID
	store, err := c.bind(tenant, owner, workspace)
	if err != nil {
		return nil, err
	}
	if c.limits.MaxFileBytes <= 0 || c.limits.MaxWorkspaceFiles <= 0 || c.limits.MaxWorkspaceBytes <= 0 {
		return nil, invalid("file_limits_unconfigured", "file catalog quota policy is not configured")
	}
	for _, upload := range uploads {
		if upload.WorkspaceID != workspace {
			return nil, invalid("invalid_file_batch", "all sources must belong to the same workspace")
		}
		if err = validateUpload(tenant, upload); err != nil {
			return nil, err
		}
		if upload.SizeBytes > c.limits.MaxFileBytes {
			return nil, invalid("file_too_large", "file exceeds the configured size limit")
		}
	}
	// For new duplicates in one batch, choose the same canonical identity even
	// when an adoption retry supplies its sources in a different order.
	order := make([]int, len(uploads))
	for i := range order {
		order[i] = i
	}
	sort.SliceStable(order, func(i, j int) bool { return uploads[order[i]].SourceID < uploads[order[j]].SourceID })
	results := make([]Record, len(uploads))
	err = store.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		if _, err := tx.Execute(ctx, `INSERT INTO runtime_file_catalog_locks (workspace_id, revision) VALUES ($1, $2) ON CONFLICT DO NOTHING`, workspace, int64(0)); err != nil {
			return err
		}
		if _, err := tx.Execute(ctx, `UPDATE runtime_file_catalog_locks SET revision = revision + 1 WHERE workspace_id = $1`, workspace); err != nil {
			return err
		}
		quota, _, err := tx.QueryOne(ctx, `SELECT COUNT(*) AS file_count, COALESCE(SUM(v.size_bytes), 0) AS total_bytes`+recordFrom+` WHERE f.workspace_id = $1 AND f.lifecycle = 'active'`, workspace)
		if err != nil {
			return err
		}
		count, _ := quota["file_count"].(int64)
		bytes, _ := quota["total_bytes"].(int64)
		for _, i := range order {
			upload := uploads[i]
			wanted := initialRecord(owner, upload)
			row, found, err := tx.QueryOne(ctx, `SELECT `+sourceColumns+sourceFrom+` WHERE s.source_id = $1`, upload.SourceID)
			if err != nil {
				return err
			}
			if found {
				record := decode(row)
				if record.File.CreatedBy != owner || record.File.WorkspaceID != workspace || record.File.Lifecycle != "active" {
					return missing()
				}
				sourceURI, _ := row["source_object_uri"].(string)
				sourceCreated, _ := row["source_created_at"].(int64)
				if sourceURI != upload.ObjectURI || sourceCreated != wanted.File.CreatedAt.UnixMilli() ||
					record.File.Name != upload.Name || record.Version.MIMEType != upload.MIMEType ||
					record.Version.SHA256 != upload.SHA256 || record.Version.SizeBytes != upload.SizeBytes {
					return conflict()
				}
				results[i] = record
				continue
			}
			row, found, err = tx.QueryOne(ctx, `SELECT `+recordColumns+recordFrom+` WHERE f.workspace_id = $1 AND f.created_by = $2 AND f.name = $3 AND f.lifecycle = 'active' AND v.sha256 = $4 AND v.size_bytes = $5 ORDER BY f.created_at DESC, f.file_id DESC LIMIT 1`, workspace, owner, upload.Name, upload.SHA256, upload.SizeBytes)
			if err != nil {
				return err
			}
			record := wanted
			if found {
				record = decode(row)
				if record.Version.MIMEType != upload.MIMEType {
					return conflict()
				}
			} else {
				if count >= int64(c.limits.MaxWorkspaceFiles) || upload.SizeBytes > c.limits.MaxWorkspaceBytes-bytes {
					return providererr.New(providererr.Denied, "file_quota_exceeded", "workspace file quota exceeded")
				}
				_, err = tx.Execute(ctx, `INSERT INTO runtime_files (file_id, workspace_id, name, created_by, created_at, head_version_id, lifecycle) VALUES ($1,$2,$3,$4,$5,$6,$7)`, wanted.File.ID, workspace, upload.Name, owner, wanted.File.CreatedAt.UnixMilli(), wanted.File.HeadVersionID, wanted.File.Lifecycle)
				if err != nil {
					return err
				}
				_, err = tx.Execute(ctx, `INSERT INTO runtime_file_versions (version_id, file_id, object_uri, mime_type, size_bytes, sha256, created_at, created_by, parent_version_id) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)`, wanted.Version.ID, wanted.File.ID, wanted.ObjectURI, upload.MIMEType, upload.SizeBytes, upload.SHA256, wanted.Version.CreatedAt.UnixMilli(), owner, "")
				if err != nil {
					return err
				}
				count++
				bytes += upload.SizeBytes
			}
			_, err = tx.Execute(ctx, `INSERT INTO runtime_file_sources (source_id, file_id, object_uri, created_at) VALUES ($1,$2,$3,$4)`, upload.SourceID, record.File.ID, upload.ObjectURI, wanted.File.CreatedAt.UnixMilli())
			if err != nil {
				return err
			}
			results[i] = record
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return results, nil
}

// FindDuplicate lets the upload boundary reuse an existing immutable source
// before writing another blob. RegisterBatch repeats this check under its lock
// to close the concurrent-upload race. A miss never authorizes a new upload.
func (c *Catalog) FindDuplicate(ctx context.Context, tenant *commonv1.TenantContext, owner, workspace, name, digest string, size int64, mimeType string) (Record, bool, error) {
	store, err := c.bind(tenant, owner, workspace)
	if err != nil {
		return Record{}, false, err
	}
	if !validName(name) || !digestPattern.MatchString(digest) || size < 0 {
		return Record{}, false, invalid("invalid_file_metadata", "invalid file name, digest or size")
	}
	if _, _, err := mime.ParseMediaType(mimeType); err != nil || len(mimeType) > 255 || strings.IndexFunc(mimeType, unicode.IsControl) >= 0 {
		return Record{}, false, invalid("invalid_file_mime", "invalid file media type")
	}
	if c.limits.MaxFileBytes <= 0 {
		return Record{}, false, invalid("file_limits_unconfigured", "file catalog quota policy is not configured")
	}
	if size > c.limits.MaxFileBytes {
		return Record{}, false, invalid("file_too_large", "file exceeds the configured size limit")
	}
	var result Record
	found := false
	err = store.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		row, ok, err := tx.QueryOne(ctx, `SELECT `+recordColumns+recordFrom+` WHERE f.workspace_id = $1 AND f.created_by = $2 AND f.name = $3 AND f.lifecycle = 'active' AND v.sha256 = $4 AND v.size_bytes = $5 ORDER BY f.created_at DESC, f.file_id DESC LIMIT 1`, workspace, owner, name, digest, size)
		if err != nil || !ok {
			return err
		}
		result = decode(row)
		if result.Version.MIMEType != mimeType {
			return conflict()
		}
		found = true
		return nil
	})
	if err != nil {
		return Record{}, false, err
	}
	return result, found, nil
}

func (c *Catalog) get(ctx context.Context, tenant *commonv1.TenantContext, owner, workspace, condition, value string) (Record, error) {
	store, err := c.bind(tenant, owner, workspace)
	if err != nil {
		return Record{}, err
	}
	var result Record
	err = store.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		row, found, err := tx.QueryOne(ctx, `SELECT `+recordColumns+sourceFrom+` WHERE f.created_by = $1 AND f.workspace_id = $2 AND f.lifecycle = 'active' AND `+condition+` = $3`, owner, workspace, value)
		if err != nil {
			return err
		}
		if !found {
			return missing()
		}
		result = decode(row)
		return nil
	})
	if err != nil {
		return Record{}, err
	}
	return result, nil
}

func (c *Catalog) GetBySourceID(ctx context.Context, tenant *commonv1.TenantContext, owner, workspace, sourceID string) (Record, error) {
	if !sourceIDPattern.MatchString(sourceID) {
		return Record{}, invalid("invalid_file_id", "invalid file identity")
	}
	return c.get(ctx, tenant, owner, workspace, "s.source_id", sourceID)
}

func (c *Catalog) Resolve(ctx context.Context, tenant *commonv1.TenantContext, owner, workspace, objectURI string) (Record, error) {
	if !strings.HasPrefix(objectURI, "obj://"+tenant.GetTenantId()+"/uploads/") {
		return Record{}, missing()
	}
	return c.get(ctx, tenant, owner, workspace, "s.object_uri", objectURI)
}

type listCursor struct {
	Tenant    string `json:"tenant"`
	CreatedAt int64  `json:"at"`
	ID        string `json:"id"`
	Owner     string `json:"owner"`
	Workspace string `json:"workspace"`
}

// List returns newest-first keyset pages. The immutable timestamp plus source ID
// is a total order: ties and newly arriving files cannot duplicate prior rows.
func (c *Catalog) List(ctx context.Context, tenant *commonv1.TenantContext, owner, workspace string, limit int, cursor string) (Page, error) {
	store, err := c.bind(tenant, owner, workspace)
	if err != nil {
		return Page{}, err
	}
	if limit == 0 {
		limit = 50
	}
	if limit < 1 || limit > 100 {
		return Page{}, invalid("invalid_file_limit", "limit must be between 1 and 100")
	}
	var after listCursor
	if cursor != "" {
		if len(cursor) > 4096 {
			return Page{}, invalid("invalid_file_cursor", "invalid file cursor")
		}
		raw, err := base64.RawURLEncoding.DecodeString(cursor)
		if err != nil || json.Unmarshal(raw, &after) != nil || !sourceIDPattern.MatchString(after.ID) || after.CreatedAt < 0 || after.Tenant != tenant.GetTenantId() || after.Owner != owner || after.Workspace != workspace {
			return Page{}, invalid("invalid_file_cursor", "invalid file cursor")
		}
	}
	page := Page{Files: []Record{}}
	err = store.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		query := `SELECT ` + recordColumns + recordFrom + ` WHERE f.created_by = $1 AND f.workspace_id = $2 AND f.lifecycle = 'active'`
		args := []any{owner, workspace}
		if cursor != "" {
			query += ` AND (f.created_at < $3 OR (f.created_at = $3 AND f.file_id < $4))`
			args = append(args, after.CreatedAt, after.ID)
		}
		query += ` ORDER BY f.created_at DESC, f.file_id DESC LIMIT ` + "$" + strconv.Itoa(len(args)+1)
		args = append(args, int64(limit+1))
		rows, err := tx.Query(ctx, query, args...)
		if err != nil {
			return err
		}
		more := len(rows) > limit
		if more {
			rows = rows[:limit]
		}
		for _, row := range rows {
			page.Files = append(page.Files, decode(row))
		}
		if more {
			last := page.Files[len(page.Files)-1].File
			encoded, err := json.Marshal(listCursor{Tenant: tenant.GetTenantId(), CreatedAt: last.CreatedAt.UnixMilli(), ID: last.ID, Owner: owner, Workspace: workspace})
			if err != nil {
				return err
			}
			page.NextCursor = base64.RawURLEncoding.EncodeToString(encoded)
		}
		return nil
	})
	return page, err
}
