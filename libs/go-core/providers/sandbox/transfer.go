package sandbox

import (
	"context"
	"time"
)

// ScopedTransferNetwork is an optional, per-object egress capability. Providers
// must persist revocation and enforce method, size and expiry in their gateway.
type ScopedTransferNetwork interface {
	GrantTransferTarget(context.Context, string, string, string, string, int64, time.Time, TransferTarget) (TransferTarget, error)
	RevokeTransferTarget(context.Context, string, string) (int64, error)
	AcknowledgeTransferTarget(context.Context, string, string) error
}

// PreparedTransfer names immutable, bounded bytes retained by one sandbox agent.
type PreparedTransfer struct {
	TransferID string `json:"transfer_id"`
	SHA256     string `json:"sha256"`
	SizeBytes  int64  `json:"size_bytes"`
}
type TransferTarget struct {
	URL     string            `json:"url"`
	Headers map[string]string `json:"headers,omitempty"`
	Proxy   string            `json:"proxy,omitempty"`
}
type TransferReceipt struct {
	ObjectKey string `json:"object_key"`
	SHA256    string `json:"sha256"`
	SizeBytes int64  `json:"size_bytes"`
}

// Transfers is optional; old/local providers continue to use bounded file/tar paths.
type Transfers interface {
	ReleaseTransfer(context.Context, string, string) error
	PrepareTransfer(context.Context, string, string) (PreparedTransfer, error)
	CommitTransfer(context.Context, string, PreparedTransfer, string, TransferTarget) (TransferReceipt, error)
	ReadTransfer(context.Context, string, string) ([]byte, error)
}

type RestoreBlock struct {
	SHA256    string         `json:"sha256"`
	SizeBytes int64          `json:"size_bytes"`
	Target    TransferTarget `json:"target"`
}
type BlockTransfers interface {
	RestoreBatch(context.Context, string, string, int64, []RestoreBlock) error
	RestoreFinish(context.Context, string, string, []FileEntry) error
}

// SnapshotTransfers hashes large files into fixed ordered blocks without returning file bytes.
type SnapshotTransfers interface {
	SnapshotManifest(context.Context, string) ([]FileEntry, error)
	PrepareRange(context.Context, string, string, int64, int64) (PreparedTransfer, error)
}
