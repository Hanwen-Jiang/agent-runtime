// Package archive 是工作区的存档与恢复（设计书改动稿 W2；D-06c，docs/plan/workspace-layer.md 第 2、3 节）。SBX-01 从 Worker 的
// agentruntime_worker.workspace.archive 搬来，数据格式不变（Worker 的 Workspace 操作照常读写同一份）：
//
//   - 存档（Snapshot）：从环境导出 tar（先写临时文件），逐个文件按 sha256 存成块（ObjectStore 租户作用域的
//     workspaces/<ws>/blobs/<sha256>，已有的不再写），再写清单（workspaces/<ws>/snapshots/<id>.json），记一行快照；只留最近
//     Keep 份，没人引用的块删掉。导出的 tar 来自沙箱，不可信：名字、链接、类型与总大小在这里再核对一遍。
//   - 恢复（Restore）：按清单从块拼出 tar（块的内容逐个核对 sha256），导入一个空的环境，再取环境的清单逐项核对；不一致是
//     transient（workspace_restore_mismatch），由 Broker 换一个环境再试一次。
//   - 业务库（迁移 205、208、210，租户作用域）：workspaces、workspace_snapshots、workspace_pushes。
package archive

import (
	"archive/tar"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path"
	"regexp"
	"sort"
	"strings"
	"sync"

	"github.com/agentruntime/agent-runtime/libs/go-core/clock"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/devutil"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/objectstore"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/ledger"
	"github.com/agentruntime/agent-runtime/services/go-sandbox-service/internal/registry"
)

// Keep 是每个工作区保留的快照份数；ManifestVersion 是清单的版本。
const (
	Keep            = 3
	ManifestVersion = 1
)

// Migrations 是工作区的迁移（与原来 Python 的 workspace.archive.MIGRATIONS 逐字相同）。
var Migrations = []relational.Migration{
	{Version: 205, Name: "workspaces", Statements: []string{
		`CREATE TABLE workspaces (
            workspace_id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            repo_url TEXT NOT NULL DEFAULT '',
            head TEXT NOT NULL DEFAULT '',
            snapshot_id TEXT NOT NULL DEFAULT '',
            size_bytes BIGINT NOT NULL DEFAULT 0,
            files BIGINT NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deleted_at TEXT NOT NULL DEFAULT ''
        )`,
		`CREATE TABLE workspace_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            seq BIGINT NOT NULL,
            manifest_key TEXT NOT NULL,
            files BIGINT NOT NULL DEFAULT 0,
            size_bytes BIGINT NOT NULL DEFAULT 0,
            new_blobs BIGINT NOT NULL DEFAULT 0,
            env_ref TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            deleted_at TEXT NOT NULL DEFAULT ''
        )`,
		"CREATE INDEX workspace_snapshots_ws ON workspace_snapshots (workspace_id, seq)",
		`CREATE TABLE workspace_pushes (
            push_key TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            branch TEXT NOT NULL,
            sha TEXT NOT NULL,
            remote TEXT NOT NULL DEFAULT '',
            approved_by TEXT NOT NULL DEFAULT '',
            operation_id TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            finished_at TEXT NOT NULL DEFAULT ''
        )`,
	}},
	{Version: 208, Name: "workspace_environment", Statements: []string{
		"ALTER TABLE workspaces ADD COLUMN environment_id TEXT NOT NULL DEFAULT ''",
		"ALTER TABLE workspaces ADD COLUMN environment TEXT NOT NULL DEFAULT ''",
		"ALTER TABLE workspaces ADD COLUMN environment_owner TEXT NOT NULL DEFAULT ''",
		"ALTER TABLE workspaces ADD COLUMN environment_deleted_at TEXT NOT NULL DEFAULT ''",
		"ALTER TABLE workspaces ADD COLUMN environment_secrets TEXT NOT NULL DEFAULT ''",
		"CREATE INDEX workspaces_environment ON workspaces (environment_id, environment_owner)",
		"ALTER TABLE workspace_snapshots ADD COLUMN blob_sources TEXT NOT NULL DEFAULT ''",
	}},
	{Version: 210, Name: "workspace_generation", Statements: []string{
		"ALTER TABLE workspaces ADD COLUMN generation BIGINT NOT NULL DEFAULT 0",
	}},
}

// BlobKey、ManifestKey 是块与清单在 ObjectStore 里的键。
func BlobKey(workspaceID, sum string) string { return "workspaces/" + workspaceID + "/blobs/" + sum }

// ManifestKey 是清单的键。
func ManifestKey(workspaceID, snapshotID string) string {
	return "workspaces/" + workspaceID + "/snapshots/" + snapshotID + ".json"
}

func newSnapshotID() string {
	b := make([]byte, 10)
	_, _ = rand.Read(b)
	return "wss-" + hex.EncodeToString(b)
}

// Workspace 是 workspaces 的一行。
type Workspace struct {
	WorkspaceID, State, CreatedAt, UpdatedAt, RepoURL, Head, SnapshotID, DeletedAt, EnvironmentID string
	SizeBytes, Files, Generation                                                                  int64
}

// Snapshot 是 workspace_snapshots 的一行。
type Snapshot struct {
	SnapshotID, WorkspaceID, ManifestKey, CreatedAt, EnvRef, DeletedAt, BlobSources string
	Seq, Files, SizeBytes, NewBlobs                                                 int64
}

var wsColumns = "workspace_id, state, created_at, updated_at, repo_url, head, snapshot_id, size_bytes, files, deleted_at, environment_id, generation"
var snapColumns = "snapshot_id, workspace_id, manifest_key, created_at, seq, files, size_bytes, new_blobs, env_ref, deleted_at, blob_sources"

func wsFromRow(r relational.Row) Workspace {
	return Workspace{WorkspaceID: ledger.Str(r, "workspace_id"), State: ledger.Str(r, "state"), CreatedAt: ledger.Str(r, "created_at"),
		UpdatedAt: ledger.Str(r, "updated_at"), RepoURL: ledger.Str(r, "repo_url"), Head: ledger.Str(r, "head"), SnapshotID: ledger.Str(r, "snapshot_id"),
		SizeBytes: ledger.Int(r, "size_bytes"), Files: ledger.Int(r, "files"), DeletedAt: ledger.Str(r, "deleted_at"),
		EnvironmentID: ledger.Str(r, "environment_id"), Generation: ledger.Int(r, "generation")}
}

func snapFromRow(r relational.Row) Snapshot {
	return Snapshot{SnapshotID: ledger.Str(r, "snapshot_id"), WorkspaceID: ledger.Str(r, "workspace_id"), ManifestKey: ledger.Str(r, "manifest_key"),
		CreatedAt: ledger.Str(r, "created_at"), Seq: ledger.Int(r, "seq"), Files: ledger.Int(r, "files"), SizeBytes: ledger.Int(r, "size_bytes"),
		NewBlobs: ledger.Int(r, "new_blobs"), EnvRef: ledger.Str(r, "env_ref"), DeletedAt: ledger.Str(r, "deleted_at"), BlobSources: ledger.Str(r, "blob_sources")}
}

// Archiver 是工作区的存档。
type Archiver struct {
	store    relational.Provider
	objects  objectstore.Provider
	clock    clock.Clock
	TmpDir   string
	Keep     int
	mu       sync.Mutex
	migrated bool
}

// New 构造。
func New(store relational.Provider, objects objectstore.Provider, c clock.Clock) *Archiver {
	if c == nil {
		c = clock.System{}
	}
	return &Archiver{store: store, objects: objects, clock: c, Keep: Keep}
}

func (a *Archiver) now() string { return ledger.ISO(a.clock.Now()) }

// Migrate 建表（幂等）。
func (a *Archiver) Migrate(ctx context.Context) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.migrated {
		return nil
	}
	if err := a.store.Migrate(ctx, Migrations); err != nil {
		return err
	}
	a.migrated = true
	return nil
}

func noPlatform(owner string) error {
	if owner == "" || owner == devutil.PlatformID {
		return providererr.New(providererr.Invalid, "unsupported_workspace", "平台作用域没有工作区")
	}
	return nil
}

func (a *Archiver) tx(ctx context.Context, owner string, readOnly bool, fn func(ctx context.Context, tx relational.Tx) error) error {
	if err := noPlatform(owner); err != nil {
		return err
	}
	if err := a.Migrate(ctx); err != nil {
		return err
	}
	store, err := a.store.Bind(&commonv1.TenantContext{TenantId: owner})
	if err != nil {
		return err
	}
	return store.Transaction(ctx, relational.TxOptions{ReadOnly: readOnly}, fn)
}

func (a *Archiver) bucket(owner string) (objectstore.Store, error) {
	if err := noPlatform(owner); err != nil {
		return nil, err
	}
	if a.objects == nil {
		return nil, providererr.New(providererr.Invalid, "workspace_archive_unavailable", "没有配工作区的对象存储")
	}
	return a.objects.Bind(&commonv1.TenantContext{TenantId: owner})
}

// Get 取工作区的行；没有时 nil。
func (a *Archiver) Get(ctx context.Context, owner, workspaceID string) (*Workspace, error) {
	var out *Workspace
	err := a.tx(ctx, owner, true, func(ctx context.Context, tx relational.Tx) error {
		row, ok, err := tx.QueryOne(ctx, "SELECT "+wsColumns+" FROM workspaces WHERE workspace_id = $1", workspaceID)
		if ok {
			w := wsFromRow(row)
			out = &w
		}
		return err
	})
	return out, err
}

// List 是没删除的工作区。
func (a *Archiver) List(ctx context.Context, owner string) ([]Workspace, error) {
	var out []Workspace
	err := a.tx(ctx, owner, true, func(ctx context.Context, tx relational.Tx) error {
		rows, err := tx.Query(ctx, "SELECT "+wsColumns+" FROM workspaces WHERE state <> 'deleted' ORDER BY workspace_id")
		for _, r := range rows {
			out = append(out, wsFromRow(r))
		}
		return err
	})
	return out, err
}

// Ensure 让工作区有一行（active）：没有时（或已删除）按 max_workspaces 核对后补一行。
func (a *Archiver) Ensure(ctx context.Context, owner, workspaceID string, limits registry.WorkspaceLimits) error {
	now := a.now()
	return a.tx(ctx, owner, false, func(ctx context.Context, tx relational.Tx) error {
		row, ok, err := tx.QueryOne(ctx, "SELECT state FROM workspaces WHERE workspace_id = $1", workspaceID)
		if err != nil {
			return err
		}
		if ok && ledger.Str(row, "state") != "deleted" {
			return nil
		}
		count, _, err := tx.QueryOne(ctx, "SELECT COUNT(*) AS n FROM workspaces WHERE state <> 'deleted'")
		if err != nil {
			return err
		}
		if limits.MaxWorkspaces > 0 && ledger.Int(count, "n") >= int64(limits.MaxWorkspaces) {
			return providererr.New(providererr.Denied, "workspace_quota_exceeded", fmt.Sprintf("同时最多 %d 个工作区，先删掉不用的", limits.MaxWorkspaces))
		}
		if ok {
			_, err = tx.Execute(ctx, "UPDATE workspaces SET state = 'active', repo_url = '', head = '', snapshot_id = '', size_bytes = 0, files = 0, "+
				"created_at = $1, updated_at = $1, deleted_at = '', environment_id = '', environment = '', environment_owner = '', "+
				"environment_deleted_at = '', environment_secrets = '' WHERE workspace_id = $2", now, workspaceID)
			return err
		}
		_, err = tx.Execute(ctx, "INSERT INTO workspaces (workspace_id, state, created_at, updated_at) VALUES ($1, 'active', $2, $2)", workspaceID, now)
		return err
	})
}

// Transition 只在当前是 from 时改成 to（冷热分层，DISK-B），返回改没改成。
func (a *Archiver) Transition(ctx context.Context, owner, workspaceID, from, to string) (bool, error) {
	changed := false
	err := a.tx(ctx, owner, false, func(ctx context.Context, tx relational.Tx) error {
		n, err := tx.Execute(ctx, "UPDATE workspaces SET state = $1 WHERE workspace_id = $2 AND state = $3", to, workspaceID, from)
		changed = n == 1
		return err
	})
	return changed, err
}

// Latest 是最近的快照；没有时 nil。
func (a *Archiver) Latest(ctx context.Context, owner, workspaceID string) (*Snapshot, error) {
	snaps, err := a.snapshots(ctx, owner, workspaceID)
	if err != nil || len(snaps) == 0 {
		return nil, err
	}
	return &snaps[0], nil
}

func (a *Archiver) snapshots(ctx context.Context, owner, workspaceID string) ([]Snapshot, error) {
	var out []Snapshot
	err := a.tx(ctx, owner, true, func(ctx context.Context, tx relational.Tx) error {
		rows, err := tx.Query(ctx, "SELECT "+snapColumns+" FROM workspace_snapshots WHERE workspace_id = $1 AND deleted_at = '' ORDER BY seq DESC", workspaceID)
		for _, r := range rows {
			out = append(out, snapFromRow(r))
		}
		return err
	})
	return out, err
}

type manifestDoc struct {
	Version        int               `json:"version"`
	WorkspaceID    string            `json:"workspace_id"`
	SnapshotID     string            `json:"snapshot_id"`
	CreatedAt      string            `json:"created_at"`
	Entries        []map[string]any  `json:"entries"`
	BlobWorkspaces map[string]string `json:"blob_workspaces,omitempty"`
}

func entryDict(e sandbox.FileEntry) map[string]any {
	out := map[string]any{"path": e.Path, "type": e.Type, "mode": e.Mode}
	if e.Type == "file" {
		out["size"], out["sha256"] = e.Size, e.SHA256
		if len(e.Chunks) > 0 {
			out["chunks"] = e.Chunks
		}
	}
	if e.Type == "symlink" {
		out["link"] = e.Link
	}
	return out
}

// Manifest 是快照的清单与“块在别的工作区之下”的那一部分（sha256 → 工作区，N-10）。
func (a *Archiver) Manifest(ctx context.Context, owner string, snap Snapshot) ([]sandbox.FileEntry, map[string]string, error) {
	bucket, err := a.bucket(owner)
	if err != nil {
		return nil, nil, err
	}
	obj, err := bucket.Get(ctx, snap.ManifestKey)
	if err != nil {
		if providererr.CategoryOf(err) == providererr.NotFound {
			return nil, nil, providererr.New(providererr.Transient, "workspace_restore_mismatch", "快照的清单不见了（被清理了？），换最新的快照再试")
		}
		return nil, nil, err
	}
	var doc struct {
		Entries        []sandbox.FileEntry `json:"entries"`
		BlobWorkspaces map[string]string   `json:"blob_workspaces"`
	}
	if err := json.Unmarshal(obj.Data, &doc); err != nil {
		return nil, nil, providererr.New(providererr.Invalid, "workspace_manifest_corrupt", "快照的清单格式不对")
	}
	if doc.BlobWorkspaces == nil {
		doc.BlobWorkspaces = map[string]string{}
	}
	return doc.Entries, doc.BlobWorkspaces, nil
}

// Blob 读一个块（核对 sha256 与大小）。
func (a *Archiver) Blob(ctx context.Context, owner, workspaceID string, entry sandbox.FileEntry, foreign map[string]string) ([]byte, error) {
	if len(entry.Chunks) > 0 {
		if _, _, err := validEntries([]sandbox.FileEntry{entry}, entry.Size); err != nil {
			return nil, err
		}
		var data []byte
		for _, b := range entry.Chunks {
			part, err := a.Blob(ctx, owner, workspaceID, sandbox.FileEntry{Path: entry.Path, Type: "file", SHA256: b.SHA256, Size: b.SizeBytes}, foreign)
			if err != nil {
				return nil, err
			}
			data = append(data, part...)
		}
		sum := sha256.Sum256(data)
		if int64(len(data)) != entry.Size || hex.EncodeToString(sum[:]) != entry.SHA256 {
			return nil, providererr.New(providererr.Transient, "workspace_restore_mismatch", "assembled file differs from manifest")
		}
		return data, nil
	}
	bucket, err := a.bucket(owner)
	if err != nil {
		return nil, err
	}
	ws := workspaceID
	if f, ok := foreign[entry.SHA256]; ok {
		ws = f
	}
	obj, err := bucket.Get(ctx, BlobKey(ws, entry.SHA256))
	if err != nil {
		if providererr.CategoryOf(err) == providererr.NotFound {
			return nil, providererr.New(providererr.Transient, "workspace_restore_mismatch", "快照里 "+entry.Path+" 的内容不见了")
		}
		return nil, err
	}
	sum := sha256.Sum256(obj.Data)
	if int64(len(obj.Data)) != entry.Size || hex.EncodeToString(sum[:]) != entry.SHA256 {
		return nil, providererr.New(providererr.Transient, "workspace_restore_mismatch", "快照里 "+entry.Path+" 的内容与清单不符")
	}
	return obj.Data, nil
}

// Snapshot 把环境里的工作区存一份快照，返回它。
func (a *Archiver) Snapshot(ctx context.Context, box sandbox.Sandbox, envID, owner, workspaceID string, maxBytes int64, envRef string, transferOpts ...TransferOptions) (Snapshot, error) {
	arch, ok := box.(sandbox.Archive)
	if !ok {
		return Snapshot{}, providererr.New(providererr.Invalid, "unsupported_archive", "这个沙箱后端不能导出、导入工作区")
	}
	bucket, err := a.bucket(owner)
	if err != nil {
		return Snapshot{}, err
	}
	known := map[string]bool{}
	foreign := map[string]string{}
	if prev, err := a.Latest(ctx, owner, workspaceID); err == nil && prev != nil {
		if entries, f, err := a.Manifest(ctx, owner, *prev); err == nil {
			foreign = f
			for _, e := range entries {
				for _, b := range e.Blocks() {
					if _, isForeign := f[b.SHA256]; !isForeign {
						known[b.SHA256] = true
					}
				}
			}
		}
	}
	var entries []sandbox.FileEntry
	var total, newBlobs int64
	_, direct := box.(sandbox.Transfers)
	directDone := false
	if direct && len(transferOpts) > 0 && transferOpts[0].Enabled {
		entries, total, newBlobs, err = a.storeDirect(ctx, box, envID, bucket, workspaceID, maxBytes, known, foreign, transferOpts[0])
		if err == nil {
			directDone = true
		} else if e, ok := err.(*providererr.Error); !ok || e.Code != "transfer_unsupported" {
			return Snapshot{}, err
		}
	}
	if !directDone {
		tmp, err := os.CreateTemp(a.TmpDir, "ar-snapshot-*.tar")
		if err != nil {
			return Snapshot{}, providererr.Wrap(providererr.Transient, "tmp_unavailable", "建不了临时文件", err)
		}
		defer os.Remove(tmp.Name())
		defer tmp.Close()
		if _, err := arch.ExportArchive(ctx, envID, tmp, maxBytes); err != nil {
			return Snapshot{}, err
		}
		if _, err := tmp.Seek(0, io.SeekStart); err != nil {
			return Snapshot{}, providererr.Wrap(providererr.Transient, "tmp_unavailable", "临时文件读不了", err)
		}
		entries, total, newBlobs, err = a.storeBlobs(ctx, bucket, workspaceID, tmp, maxBytes, known, foreign)
		if err != nil {
			return Snapshot{}, err
		}
	}
	used := map[string]string{}
	var files int64
	for _, e := range entries {
		if e.Type == "file" {
			files++
			for _, b := range e.Blocks() {
				if ws, ok := foreign[b.SHA256]; ok {
					used[b.SHA256] = ws
				}
			}
		}
	}
	id := newSnapshotID()
	now := a.now()
	snap := Snapshot{SnapshotID: id, WorkspaceID: workspaceID, ManifestKey: ManifestKey(workspaceID, id), CreatedAt: now, Files: files,
		SizeBytes: total, NewBlobs: newBlobs, EnvRef: envRef, BlobSources: sources(used)}
	sort.Slice(entries, func(i, j int) bool { return entries[i].Path < entries[j].Path })
	doc := manifestDoc{Version: ManifestVersion, WorkspaceID: workspaceID, SnapshotID: id, CreatedAt: now}
	for _, e := range entries {
		doc.Entries = append(doc.Entries, entryDict(e))
	}
	if len(used) > 0 {
		doc.BlobWorkspaces = used
	}
	body, _ := json.Marshal(doc)
	if _, err := bucket.Put(ctx, snap.ManifestKey, body, objectstore.PutOptions{ContentType: "application/json"}); err != nil {
		return Snapshot{}, err
	}
	err = a.tx(ctx, owner, false, func(ctx context.Context, tx relational.Tx) error {
		row, _, err := tx.QueryOne(ctx, "SELECT MAX(seq) AS s FROM workspace_snapshots WHERE workspace_id = $1", workspaceID)
		if err != nil {
			return err
		}
		snap.Seq = ledger.Int(row, "s") + 1
		if _, err := tx.Execute(ctx, "INSERT INTO workspace_snapshots ("+snapColumns+") VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
			snap.SnapshotID, snap.WorkspaceID, snap.ManifestKey, snap.CreatedAt, snap.Seq, snap.Files, snap.SizeBytes, snap.NewBlobs, snap.EnvRef,
			snap.DeletedAt, snap.BlobSources); err != nil {
			return err
		}
		_, err = tx.Execute(ctx, "UPDATE workspaces SET snapshot_id = $1, size_bytes = $2, files = $3, updated_at = $4 WHERE workspace_id = $5",
			id, total, files, now, workspaceID)
		return err
	})
	if err != nil {
		return Snapshot{}, err
	}
	return snap, a.prune(ctx, owner, workspaceID)
}

func (a *Archiver) storeBlobs(ctx context.Context, bucket objectstore.Store, workspaceID string, src io.Reader, maxBytes int64,
	known map[string]bool, foreign map[string]string) ([]sandbox.FileEntry, int64, int64, error) {
	var entries []sandbox.FileEntry
	seen := map[string]bool{}
	var total, newBlobs int64
	tr := tar.NewReader(src)
	for {
		h, err := tr.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return nil, 0, 0, providererr.New(providererr.Transient, "workspace_archive_truncated", "导出的存档不是完整的 tar")
		}
		rel, err := CleanName(h.Name)
		if err != nil {
			return nil, 0, 0, err
		}
		if rel == "." || rel == ".agentruntime" {
			continue
		}
		if seen[rel] {
			return nil, 0, 0, providererr.New(providererr.Invalid, "invalid_archive", "导出的存档里有重名的项")
		}
		seen[rel] = true
		mode := int(h.Mode & 0o777)
		switch h.Typeflag {
		case tar.TypeDir:
			entries = append(entries, sandbox.FileEntry{Path: rel, Type: "dir", Mode: mode})
		case tar.TypeSymlink:
			if !LinkInside(rel, h.Linkname) {
				continue // 指出工作区的链接不存
			}
			entries = append(entries, sandbox.FileEntry{Path: rel, Type: "symlink", Link: h.Linkname})
		case tar.TypeReg:
			total += h.Size
			if total > maxBytes {
				return nil, 0, 0, providererr.New(providererr.Invalid, "too_large", fmt.Sprintf("工作区超过上限 %d 字节", maxBytes))
			}
			data, err := io.ReadAll(tr)
			if err != nil || int64(len(data)) != h.Size {
				return nil, 0, 0, providererr.New(providererr.Transient, "workspace_archive_truncated", "导出的存档不完整")
			}
			s := sha256.Sum256(data)
			sum := hex.EncodeToString(s[:])
			if _, isForeign := foreign[sum]; !known[sum] && !isForeign {
				key := BlobKey(workspaceID, sum)
				if _, err := bucket.Head(ctx, key); err != nil {
					if providererr.CategoryOf(err) != providererr.NotFound {
						return nil, 0, 0, err
					}
					if _, err := bucket.Put(ctx, key, data, objectstore.PutOptions{}); err != nil {
						return nil, 0, 0, err
					}
					newBlobs++
				}
				known[sum] = true
			}
			entries = append(entries, sandbox.FileEntry{Path: rel, Type: "file", Mode: mode, Size: int64(len(data)), SHA256: sum})
		default:
			return nil, 0, 0, providererr.New(providererr.Invalid, "invalid_archive", "导出的存档里只能有普通文件、目录与符号链接")
		}
	}
	return entries, total, newBlobs, nil
}

// Restore 把最近的快照恢复进一个空的环境；没有快照时什么也不做，返回 nil。
func (a *Archiver) Restore(ctx context.Context, box sandbox.Sandbox, envID, owner, workspaceID string, maxBytes int64, transferOpts ...TransferOptions) (*Snapshot, error) {
	arch, ok := box.(sandbox.Archive)
	if !ok {
		return nil, providererr.New(providererr.Invalid, "unsupported_archive", "这个沙箱后端不能导出、导入工作区")
	}
	snap, err := a.Latest(ctx, owner, workspaceID)
	if err != nil || snap == nil {
		return nil, err
	}
	entries, foreign, err := a.Manifest(ctx, owner, *snap)
	if err != nil {
		return nil, err
	}
	if direct, ok := box.(sandbox.BlockTransfers); ok && len(transferOpts) > 0 && transferOpts[0].Enabled && directSizes(entries) {
		if err := a.restoreDirect(ctx, direct, envID, owner, workspaceID, entries, foreign, max(maxBytes, snap.SizeBytes, 1), transferOpts[0]); err != nil {
			return nil, err
		}
		got, err := arch.Manifest(ctx, envID)
		if err != nil {
			return nil, err
		}
		if !SameShape(got, entries) {
			return nil, providererr.New(providererr.Transient, "workspace_restore_mismatch", "restored manifest mismatch")
		}
		return snap, nil
	}
	sorted := append([]sandbox.FileEntry(nil), entries...)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i].Path < sorted[j].Path })
	tmp, err := os.CreateTemp(a.TmpDir, "ar-restore-*.tar")
	if err != nil {
		return nil, providererr.Wrap(providererr.Transient, "tmp_unavailable", "建不了临时文件", err)
	}
	defer os.Remove(tmp.Name())
	defer tmp.Close()
	tw := tar.NewWriter(tmp)
	for _, e := range sorted {
		h := &tar.Header{Name: e.Path, Mode: int64(e.Mode & 0o777), Format: tar.FormatPAX}
		switch e.Type {
		case "dir":
			h.Typeflag = tar.TypeDir
			if err := tw.WriteHeader(h); err != nil {
				return nil, err
			}
		case "symlink":
			h.Typeflag, h.Linkname, h.Mode = tar.TypeSymlink, e.Link, 0o777
			if err := tw.WriteHeader(h); err != nil {
				return nil, err
			}
		default:
			data, err := a.Blob(ctx, owner, workspaceID, e, foreign)
			if err != nil {
				return nil, err
			}
			h.Typeflag, h.Size = tar.TypeReg, int64(len(data))
			if err := tw.WriteHeader(h); err != nil {
				return nil, err
			}
			if _, err := tw.Write(data); err != nil {
				return nil, err
			}
		}
	}
	if err := tw.Close(); err != nil {
		return nil, err
	}
	size, err := tmp.Seek(0, io.SeekCurrent)
	if err != nil {
		return nil, err
	}
	if _, err := tmp.Seek(0, io.SeekStart); err != nil {
		return nil, err
	}
	limit := max(maxBytes, snap.SizeBytes, 1)
	if err := arch.ImportArchive(ctx, envID, tmp, size, limit); err != nil {
		return nil, err
	}
	got, err := arch.Manifest(ctx, envID)
	if err != nil {
		return nil, err
	}
	if !SameShape(got, entries) {
		return nil, providererr.New(providererr.Transient, "workspace_restore_mismatch", "恢复后环境里的文件与快照的清单不一致")
	}
	return snap, nil
}

// SameShape 核对恢复结果：路径、类型、大小、内容、链接（权限位只恢复、不核对）。
func SameShape(a, b []sandbox.FileEntry) bool {
	key := func(list []sandbox.FileEntry) []string {
		out := make([]string, 0, len(list))
		for _, e := range list {
			size := int64(0)
			if e.Type == "file" {
				size = e.Size
			}
			out = append(out, fmt.Sprintf("%s\x00%s\x00%d\x00%s\x00%s", e.Path, e.Type, size, e.SHA256, e.Link))
		}
		sort.Strings(out)
		return out
	}
	ka, kb := key(a), key(b)
	if len(ka) != len(kb) {
		return false
	}
	for i := range ka {
		if ka[i] != kb[i] {
			return false
		}
	}
	return true
}

func (a *Archiver) prune(ctx context.Context, owner, workspaceID string) error {
	snaps, err := a.snapshots(ctx, owner, workspaceID)
	if err != nil {
		return err
	}
	keep := max(1, a.Keep)
	if len(snaps) <= keep {
		return a.deleteUnreferenced(ctx, owner, workspaceID, snaps, nil)
	}
	bucket, err := a.bucket(owner)
	if err != nil {
		return err
	}
	kept, dropped := snaps[:keep], snaps[keep:]
	srcs := map[string]bool{}
	now := a.now()
	for _, s := range dropped {
		for _, n := range strings.Split(s.BlobSources, ",") {
			if n != "" {
				srcs[n] = true
			}
		}
		if _, err := bucket.Delete(ctx, s.ManifestKey); err != nil {
			return err
		}
		if err := a.tx(ctx, owner, false, func(ctx context.Context, tx relational.Tx) error {
			_, err := tx.Execute(ctx, "UPDATE workspace_snapshots SET deleted_at = $1 WHERE snapshot_id = $2", now, s.SnapshotID)
			return err
		}); err != nil {
			return err
		}
	}
	return a.deleteUnreferenced(ctx, owner, workspaceID, kept, srcs)
}

func (a *Archiver) deleteUnreferenced(ctx context.Context, owner, workspaceID string, kept []Snapshot, srcs map[string]bool) error {
	referenced := map[string]bool{}
	for _, s := range kept {
		entries, foreign, err := a.Manifest(ctx, owner, s)
		if err != nil {
			return err
		}
		for _, e := range entries {
			for _, b := range e.Blocks() {
				if _, isForeign := foreign[b.SHA256]; !isForeign {
					referenced[b.SHA256] = true
				}
			}
		}
	}
	shared, err := a.shared(ctx, owner, workspaceID)
	if err != nil {
		return err
	}
	for k := range shared {
		referenced[k] = true
	}
	if err := a.deleteBlobs(ctx, owner, workspaceID, referenced); err != nil {
		return err
	}
	names := make([]string, 0, len(srcs))
	for n := range srcs {
		names = append(names, n)
	}
	sort.Strings(names)
	for _, src := range names {
		w, err := a.Get(ctx, owner, src)
		if err != nil {
			return err
		}
		if w == nil || w.State == "deleted" {
			s, err := a.shared(ctx, owner, src)
			if err != nil {
				return err
			}
			if err := a.deleteBlobs(ctx, owner, src, s); err != nil {
				return err
			}
		}
	}
	return nil
}

// shared 是别的工作区的快照引用的、放在 workspaceID 之下的块（复制来的，N-10）。
func (a *Archiver) shared(ctx context.Context, owner, workspaceID string) (map[string]bool, error) {
	var rows []Snapshot
	err := a.tx(ctx, owner, true, func(ctx context.Context, tx relational.Tx) error {
		rs, err := tx.Query(ctx, "SELECT "+snapColumns+" FROM workspace_snapshots WHERE deleted_at = '' AND workspace_id <> $1 AND blob_sources LIKE $2",
			workspaceID, "%,"+workspaceID+",%")
		for _, r := range rs {
			rows = append(rows, snapFromRow(r))
		}
		return err
	})
	out := map[string]bool{}
	for _, s := range rows {
		if _, foreign, err := a.Manifest(ctx, owner, s); err == nil {
			for sum, ws := range foreign {
				if ws == workspaceID {
					out[sum] = true
				}
			}
		}
	}
	return out, err
}

func (a *Archiver) keys(ctx context.Context, bucket objectstore.Store, prefix string) ([]string, error) {
	var out []string
	cursor := ""
	for {
		page, err := bucket.List(ctx, objectstore.ListOptions{Prefix: prefix, Limit: 1000, Cursor: cursor})
		if err != nil {
			return nil, err
		}
		for _, it := range page.Items {
			out = append(out, it.Key)
		}
		if page.NextCursor == "" {
			return out, nil
		}
		cursor = page.NextCursor
	}
}

func (a *Archiver) deleteBlobs(ctx context.Context, owner, workspaceID string, keep map[string]bool) error {
	bucket, err := a.bucket(owner)
	if err != nil {
		return err
	}
	prefix := "workspaces/" + workspaceID + "/blobs/"
	keys, err := a.keys(ctx, bucket, prefix)
	if err != nil {
		return err
	}
	for _, k := range keys {
		if !keep[strings.TrimPrefix(k, prefix)] {
			if _, err := bucket.Delete(ctx, k); err != nil {
				return err
			}
		}
	}
	return nil
}

// Delete 删掉工作区的存档（块、清单）与快照，行记为 deleted（租户删除时）。别的工作区复制来的快照还引用的块先留着。
func (a *Archiver) Delete(ctx context.Context, owner, workspaceID string) error {
	bucket, err := a.bucket(owner)
	if err != nil {
		return err
	}
	shared, err := a.shared(ctx, owner, workspaceID)
	if err != nil {
		return err
	}
	keys, err := a.keys(ctx, bucket, "workspaces/"+workspaceID+"/")
	if err != nil {
		return err
	}
	blobs := "workspaces/" + workspaceID + "/blobs/"
	for _, k := range keys {
		if strings.HasPrefix(k, blobs) && shared[strings.TrimPrefix(k, blobs)] {
			continue
		}
		if _, err := bucket.Delete(ctx, k); err != nil {
			return err
		}
	}
	now := a.now()
	return a.tx(ctx, owner, false, func(ctx context.Context, tx relational.Tx) error {
		if _, err := tx.Execute(ctx, "UPDATE workspace_snapshots SET deleted_at = $1 WHERE workspace_id = $2 AND deleted_at = ''", now, workspaceID); err != nil {
			return err
		}
		_, err := tx.Execute(ctx, "UPDATE workspaces SET state = 'deleted', snapshot_id = '', size_bytes = 0, files = 0, updated_at = $1, deleted_at = $1 WHERE workspace_id = $2", now, workspaceID)
		return err
	})
}

func sources(located map[string]string) string {
	set := map[string]bool{}
	for _, ws := range located {
		set[ws] = true
	}
	if len(set) == 0 {
		return ""
	}
	names := make([]string, 0, len(set))
	for n := range set {
		names = append(names, n)
	}
	sort.Strings(names)
	return "," + strings.Join(names, ",") + ","
}

var drive = regexp.MustCompile(`^[A-Za-z]:`)

// CleanName 把 tar 里的名字规范化成相对工作目录的路径；不合法时 invalid（同 Python 的 workdir.clean_name）。
func CleanName(name string) (string, error) {
	if name == "" || strings.ContainsAny(name, "\\\x00") || strings.HasPrefix(name, "/") || drive.MatchString(name) {
		return "", providererr.New(providererr.Invalid, "invalid_path", "存档里有绝对路径或不合法的名字")
	}
	rel := path.Clean(strings.TrimRight(name, "/"))
	if rel == ".." || strings.HasPrefix(rel, "../") {
		return "", providererr.New(providererr.Invalid, "invalid_path", "存档里有出工作目录的路径")
	}
	return rel, nil
}

// LinkInside 判断 rel（相对工作目录）处的链接目标 target 是否还在工作目录里。
func LinkInside(rel, target string) bool {
	if target == "" || strings.HasPrefix(target, "/") || strings.ContainsAny(target, "\\\x00") || drive.MatchString(target) {
		return false
	}
	joined := path.Clean(path.Join(path.Dir(rel), target))
	return joined != ".." && !strings.HasPrefix(joined, "../")
}
