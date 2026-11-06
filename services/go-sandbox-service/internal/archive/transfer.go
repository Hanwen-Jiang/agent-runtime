package archive

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/objectstore"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	"time"
)

type TransferOptions struct {
	Enabled     bool
	OpenNetwork func(context.Context, string) (string, func() error, error)
}

func directSizes(entries []sandbox.FileEntry) bool {
	for _, e := range entries {
		if e.Type == "file" && len(e.Chunks) == 0 && e.Size > 64<<20 {
			return false
		}
	}
	return true
}
func validEntries(entries []sandbox.FileEntry, maxBytes int64) ([]sandbox.FileEntry, int64, error) {
	var out []sandbox.FileEntry
	var total int64
	seen := map[string]bool{}
	for _, e := range entries {
		p, err := CleanName(e.Path)
		if err != nil {
			return nil, 0, err
		}
		if p == "." || p == ".agentruntime" {
			continue
		}
		if seen[p] {
			return nil, 0, providererr.New(providererr.Invalid, "invalid_archive", "duplicate manifest entry")
		}
		seen[p] = true
		e.Path = p
		e.Mode &= 0777
		switch e.Type {
		case "file":
			raw, err := hex.DecodeString(e.SHA256)
			if err != nil || len(raw) != 32 || hex.EncodeToString(raw) != e.SHA256 || e.Size < 0 {
				return nil, 0, providererr.New(providererr.Invalid, "invalid_archive", "invalid file digest or size")
			}
			var chunkBytes int64
			for _, b := range e.Chunks {
				raw, err := hex.DecodeString(b.SHA256)
				if err != nil || len(raw) != 32 || hex.EncodeToString(raw) != b.SHA256 || b.SizeBytes <= 0 || b.SizeBytes > 4<<20 || b.SizeBytes > e.Size-chunkBytes {
					return nil, 0, providererr.New(providererr.Invalid, "invalid_archive", "invalid file chunks")
				}
				chunkBytes += b.SizeBytes
			}
			if len(e.Chunks) > 0 && chunkBytes != e.Size {
				return nil, 0, providererr.New(providererr.Invalid, "invalid_archive", "chunk sizes differ from file size")
			}
			if e.Size > maxBytes-total {
				return nil, 0, providererr.New(providererr.Invalid, "too_large", "workspace exceeds limit")
			}
			total += e.Size
		case "dir":
		case "symlink":
			if !LinkInside(e.Path, e.Link) {
				continue
			}
		default:
			return nil, 0, providererr.New(providererr.Invalid, "invalid_archive", "unsupported manifest entry")
		}
		out = append(out, e)
	}
	return out, total, nil
}
func network(ctx context.Context, opt TransferOptions, url string) (string, func() error, error) {
	if opt.OpenNetwork == nil {
		return "", func() error { return nil }, nil
	}
	return opt.OpenNetwork(ctx, url)
}
func (a *Archiver) storeDirect(ctx context.Context, box sandbox.Sandbox, envID string, bucket objectstore.Store, workspaceID string, maxBytes int64, known map[string]bool, foreign map[string]string, opt TransferOptions) ([]sandbox.FileEntry, int64, int64, error) {
	manifest := box.(sandbox.Archive).Manifest
	chunked, canChunk := box.(sandbox.SnapshotTransfers)
	if canChunk {
		manifest = chunked.SnapshotManifest
	}
	entries, err := manifest(ctx, envID)
	if err != nil {
		return nil, 0, 0, err
	}
	if !directSizes(entries) {
		return nil, 0, 0, providererr.New(providererr.Invalid, "transfer_unsupported", "workspace has oversized blocks")
	}
	entries, total, err := validEntries(entries, maxBytes)
	if err != nil {
		return nil, 0, 0, err
	}
	transfers := box.(sandbox.Transfers)
	var added int64
	checked := map[string]bool{}
	for _, file := range entries {
		var offset int64
		for _, block := range file.Blocks() {
			e := sandbox.FileEntry{Path: file.Path, Type: "file", SHA256: block.SHA256, Size: block.SizeBytes}
			at := offset
			offset += e.Size
			if checked[e.SHA256] {
				continue
			}
			checked[e.SHA256] = true
			ws := workspaceID
			if src, ok := foreign[e.SHA256]; ok {
				ws = src
			}
			key := BlobKey(ws, e.SHA256)
			if info, err := bucket.Head(ctx, key); err == nil {
				if info.Size != e.Size {
					return nil, 0, 0, providererr.New(providererr.Transient, "workspace_restore_mismatch", "stored blob size mismatch")
				}
				known[e.SHA256] = true
				continue
			} else if providererr.CategoryOf(err) != providererr.NotFound {
				return nil, 0, 0, err
			}
			// Missing inherited blocks are uploaded into this workspace's own namespace.
			delete(foreign, e.SHA256)
			key = BlobKey(workspaceID, e.SHA256)
			var p sandbox.PreparedTransfer
			var err error
			if len(file.Chunks) > 0 {
				if !canChunk {
					return nil, 0, 0, providererr.New(providererr.Invalid, "transfer_unsupported", "chunk preparation unavailable")
				}
				p, err = chunked.PrepareRange(ctx, envID, e.Path, at, e.Size)
			} else {
				p, err = transfers.PrepareTransfer(ctx, envID, e.Path)
			}
			if err != nil {
				return nil, 0, 0, err
			}
			uploadErr := func() error {
				defer transfers.ReleaseTransfer(context.WithoutCancel(ctx), envID, p.TransferID)
				if p.SHA256 != e.SHA256 || p.SizeBytes != e.Size {
					return providererr.New(providererr.Transient, "workspace_changed", "workspace changed while snapshotting")
				}
				signed, err := bucket.SignedUpload(ctx, key, 5*time.Minute, p.SHA256, p.SizeBytes)
				if err != nil {
					return err
				}
				proxy, close, err := network(ctx, opt, signed.URL)
				if err != nil {
					return err
				}
				_, err = transfers.CommitTransfer(ctx, envID, p, key, sandbox.TransferTarget{URL: signed.URL, Headers: signed.Headers, Proxy: proxy})
				closeErr := close()
				if err != nil {
					return err
				}
				if closeErr != nil {
					return closeErr
				}
				info, err := bucket.Head(ctx, key)
				if err != nil {
					return err
				}
				if info.Size != p.SizeBytes {
					return providererr.New(providererr.Transient, "workspace_restore_mismatch", "uploaded blob size mismatch")
				}
				return nil
			}()
			if uploadErr != nil {
				return nil, 0, 0, uploadErr
			}
			known[e.SHA256] = true
			added++
		}
	}
	final, err := manifest(ctx, envID)
	if err != nil {
		return nil, 0, 0, err
	}
	final, _, err = validEntries(final, maxBytes)
	if err != nil {
		return nil, 0, 0, err
	}
	if !SameShape(entries, final) {
		return nil, 0, 0, providererr.New(providererr.Transient, "workspace_changed", "workspace changed while snapshotting")
	}
	return entries, total, added, nil
}
func (a *Archiver) restoreDirect(ctx context.Context, box sandbox.BlockTransfers, envID, owner, workspaceID string, entries []sandbox.FileEntry, foreign map[string]string, maxBytes int64, opt TransferOptions) error {
	entries, _, err := validEntries(entries, maxBytes)
	if err != nil {
		return err
	}
	bucket, err := a.bucket(owner)
	if err != nil {
		return err
	}
	raw := make([]byte, 16)
	if _, err = rand.Read(raw); err != nil {
		return err
	}
	id := hex.EncodeToString(raw)
	var files []sandbox.FileEntry
	seen := map[string]bool{}
	for _, e := range entries {
		for _, b := range e.Blocks() {
			if !seen[b.SHA256] {
				files = append(files, sandbox.FileEntry{SHA256: b.SHA256, Size: b.SizeBytes})
				seen[b.SHA256] = true
			}
		}
	}
	// At most 16 URLs are issued at once; every batch gets fresh five-minute signatures.
	for start := 0; start < len(files) || start == 0; start += 16 {
		end := min(start+16, len(files))
		var blocks []sandbox.RestoreBlock
		var cleanups []func() error
		batchErr := func() (err error) {
			defer func() {
				for _, close := range cleanups {
					if closeErr := close(); err == nil {
						err = closeErr
					}
				}
			}()
			for _, e := range files[start:end] {
				ws := workspaceID
				if src, ok := foreign[e.SHA256]; ok {
					ws = src
				}
				url, err := bucket.TemporaryURL(ctx, BlobKey(ws, e.SHA256), 5*time.Minute, "GET")
				if err != nil {
					return err
				}
				proxy, close, err := network(ctx, opt, url)
				if err != nil {
					return err
				}
				cleanups = append(cleanups, close)
				blocks = append(blocks, sandbox.RestoreBlock{SHA256: e.SHA256, SizeBytes: e.Size, Target: sandbox.TransferTarget{URL: url, Proxy: proxy}})
			}
			return box.RestoreBatch(ctx, envID, id, maxBytes, blocks)
		}()
		if batchErr != nil {
			return batchErr
		}
		if end == len(files) {
			break
		}
	}
	return box.RestoreFinish(ctx, envID, id, entries)
}
