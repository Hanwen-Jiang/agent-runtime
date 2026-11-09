// Package files owns the Runtime file catalog. Blob verification belongs to the
// authenticated upload/adoption boundary; the catalog owns identity and access.
package files

import "time"

// UploadRecord is an already verified, immutable source object. No user URL is
// stored: downloads are resolved through the authenticated application route.
type UploadRecord struct {
	SourceID    string
	WorkspaceID string
	Name        string
	ObjectURI   string
	SHA256      string
	MIMEType    string
	SizeBytes   int64
	CreatedAt   time.Time
}

// File is a logical identity. M0.1 only publishes its initial version.
type File struct {
	ID            string    `json:"id"`
	WorkspaceID   string    `json:"workspace_id"`
	Name          string    `json:"name"`
	CreatedBy     string    `json:"created_by"`
	CreatedAt     time.Time `json:"created_at"`
	HeadVersionID string    `json:"head_version_id"`
	Lifecycle     string    `json:"lifecycle"`
}

// FileVersion describes immutable source bytes. ParentVersionID is reserved for
// the version model; M0.1 exposes no API for editing or publishing later versions.
type FileVersion struct {
	ID              string    `json:"id"`
	FileID          string    `json:"file_id"`
	MIMEType        string    `json:"mime_type"`
	SizeBytes       int64     `json:"size_bytes"`
	SHA256          string    `json:"sha256"`
	CreatedAt       time.Time `json:"created_at"`
	CreatedBy       string    `json:"created_by"`
	ParentVersionID string    `json:"parent_version_id,omitempty"`
}

// Record keeps the storage reference internal, separate from the public schema.
type Record struct {
	File      File        `json:"file"`
	Version   FileVersion `json:"version"`
	ObjectURI string      `json:"-"`
}

type Page struct {
	Files      []Record `json:"files"`
	NextCursor string   `json:"next_cursor"`
}

// Limits are supplied by the service's frozen quota policy. Explicit zero
// limits are invalid, never an unlimited policy.
type Limits struct {
	MaxFileBytes      int64
	MaxWorkspaceFiles int
	MaxWorkspaceBytes int64
}

type Options struct {
	Limits Limits
}

// DefaultLimits is the frozen M0 policy for one Runtime session workspace.
// Project-wide quotas require a project catalog and are outside this slice.
func DefaultLimits() Limits {
	return Limits{MaxFileBytes: 50 << 20, MaxWorkspaceFiles: 500, MaxWorkspaceBytes: 2 << 30}
}
