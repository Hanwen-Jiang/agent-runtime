package archive

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/objectstore"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	"strings"
	"sync"
	"time"
)

type TransferTargetRequest struct {
	SizeBytes int64
	Target    sandbox.TransferTarget
}

type TransferOptions struct {
	Enabled     bool
	OpenNetwork func(context.Context, string) (string, func() error, error)
	OpenTarget  func(context.Context, string, int64, sandbox.TransferTarget) (sandbox.TransferTarget, func() error, error)
	OpenTargets func(context.Context, string, []TransferTargetRequest) ([]sandbox.TransferTarget, func() error, error)
}

func runParallel(count, limit int, fn func(int) error) error {
	if count == 0 {
		return nil
	}
	if limit <= 0 || limit > count {
		limit = count
	}
	sem := make(chan struct{}, limit)
	var (
		wg       sync.WaitGroup
		mu       sync.Mutex
		firstErr error
	)
	for i := 0; i < count; i++ {
		wg.Add(1)
		sem <- struct{}{}
		go func(idx int) {
			defer wg.Done()
			defer func() { <-sem }()
			if err := fn(idx); err != nil {
				mu.Lock()
				if firstErr == nil {
					firstErr = err
				}
				mu.Unlock()
			}
		}(i)
	}
	wg.Wait()
	return firstErr
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
	seen := map[string]string{}
	for _, e := range entries {
		p, err := CleanName(e.Path)
		if err != nil {
			return nil, 0, err
		}
		if p == "." || p == ".agentruntime" {
			continue
		}
		if _, exists := seen[p]; exists {
			return nil, 0, providererr.New(providererr.Invalid, "invalid_archive", "duplicate manifest entry")
		}
		seen[p] = e.Type
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
	for name := range seen {
		parts := strings.Split(name, "/")
		for i := 1; i < len(parts); i++ {
			if kind, exists := seen[strings.Join(parts[:i], "/")]; exists && kind != "dir" {
				return nil, 0, providererr.New(providererr.Invalid, "invalid_archive", "non-directory manifest parent")
			}
		}
	}
	return out, total, nil
}
func network(ctx context.Context, opt TransferOptions, method string, size int64, target sandbox.TransferTarget) (sandbox.TransferTarget, func() error, error) {
	if opt.OpenTarget != nil {
		return opt.OpenTarget(ctx, method, size, target)
	}
	if opt.OpenNetwork == nil {
		return target, func() error { return nil }, nil
	}
	proxy, close, err := opt.OpenNetwork(ctx, target.URL)
	target.Proxy = proxy
	return target, close, err
}
type directBlock struct {
	path    string
	sha256  string
	size    int64
	offset  int64
	chunked bool
}

func (a *Archiver) storeDirect(ctx context.Context, box sandbox.Sandbox, envID string, bucket objectstore.Store, workspaceID string, maxBytes int64, known map[string]bool, foreign map[string]string, opt TransferOptions) ([]sandbox.FileEntry, int64, int64, error) {
	manifest := box.(sandbox.Archive).Manifest
	chunked, canChunk := box.(sandbox.SnapshotTransfers)
	if canChunk && a.ChunkedSnapshots {
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
	checked := map[string]bool{}
	var toCheck []directBlock
	for _, file := range entries {
		var offset int64
		for _, block := range file.Blocks() {
			at := offset
			offset += block.SizeBytes
			if checked[block.SHA256] {
				continue
			}
			checked[block.SHA256] = true
			if _, isForeign := foreign[block.SHA256]; !isForeign && known[block.SHA256] {
				continue
			}
			toCheck = append(toCheck, directBlock{
				path:    file.Path,
				sha256:  block.SHA256,
				size:    block.SizeBytes,
				offset:  at,
				chunked: len(file.Chunks) > 0,
			})
		}
	}

	needUpload := make([]bool, len(toCheck))
	deleteForeign := make([]bool, len(toCheck))
	if err := runParallel(len(toCheck), 12, func(i int) error {
		b := toCheck[i]
		ws := workspaceID
		if src, ok := foreign[b.sha256]; ok {
			ws = src
		}
		key := BlobKey(ws, b.sha256)
		if info, err := bucket.Head(ctx, key); err == nil {
			if info.Size != b.size {
				return providererr.New(providererr.Transient, "workspace_restore_mismatch", "stored blob size mismatch")
			}
			return nil
		} else if providererr.CategoryOf(err) != providererr.NotFound {
			return err
		}
		needUpload[i] = true
		if _, ok := foreign[b.sha256]; ok {
			deleteForeign[i] = true
		}
		return nil
	}); err != nil {
		return nil, 0, 0, err
	}

	var missing []directBlock
	for i, b := range toCheck {
		if deleteForeign[i] {
			delete(foreign, b.sha256)
		}
		if needUpload[i] {
			missing = append(missing, b)
		} else {
			known[b.sha256] = true
		}
	}

	var added int64
	for start := 0; start < len(missing); start += 16 {
		if err := ctx.Err(); err != nil {
			return nil, 0, 0, err
		}
		batch := missing[start:min(start+16, len(missing))]
		prepared := make([]sandbox.PreparedTransfer, len(batch))
		prepErr := runParallel(len(batch), 8, func(i int) error {
			b := batch[i]
			var (
				p   sandbox.PreparedTransfer
				err error
			)
			if b.chunked {
				if !canChunk {
					return providererr.New(providererr.Invalid, "transfer_unsupported", "chunk preparation unavailable")
				}
				p, err = chunked.PrepareRange(ctx, envID, b.path, b.offset, b.size)
			} else {
				p, err = transfers.PrepareTransfer(ctx, envID, b.path)
			}
			if err != nil {
				return err
			}
			prepared[i] = p
			return nil
		})
		releaseBatch := func() {
			relCtx := context.WithoutCancel(ctx)
			_ = runParallel(len(prepared), 8, func(i int) error {
				if prepared[i].TransferID != "" {
					return transfers.ReleaseTransfer(relCtx, envID, prepared[i].TransferID)
				}
				return nil
			})
		}
		if prepErr != nil {
			releaseBatch()
			return nil, 0, 0, prepErr
		}
		for i, b := range batch {
			if prepared[i].SHA256 != b.sha256 || prepared[i].SizeBytes != b.size {
				releaseBatch()
				return nil, 0, 0, providererr.New(providererr.Transient, "workspace_changed", "workspace changed while snapshotting")
			}
		}

		keys := make([]string, len(batch))
		for i, b := range batch {
			keys[i] = BlobKey(workspaceID, b.sha256)
		}
		var uploadErr error
		if len(batch) > 1 && opt.OpenTargets != nil {
			uploadErr = a.uploadPreparedBatch(ctx, transfers, envID, bucket, keys, prepared, opt)
		} else {
			uploadErr = runParallel(len(batch), 8, func(i int) error {
				return uploadPreparedBlockWithAttempts(ctx, transfers, envID, bucket, keys[i], prepared[i], opt, 3, 1)
			})
		}
		releaseBatch()
		if uploadErr != nil {
			return nil, 0, 0, uploadErr
		}
		for _, b := range batch {
			known[b.sha256] = true
		}
		added += int64(len(batch))
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

func (a *Archiver) uploadPreparedBatch(ctx context.Context, transfers sandbox.Transfers, envID string, bucket objectstore.Store, keys []string, prepared []sandbox.PreparedTransfer, opt TransferOptions) error {
	reqs := make([]TransferTargetRequest, len(prepared))
	for i, p := range prepared {
		signed, err := bucket.SignedUpload(ctx, keys[i], 5*time.Minute, p.SHA256, p.SizeBytes)
		if err != nil {
			return err
		}
		reqs[i] = TransferTargetRequest{
			SizeBytes: p.SizeBytes,
			Target:    sandbox.TransferTarget{URL: signed.URL, Headers: signed.Headers},
		}
	}
	targets, closeBatch, err := opt.OpenTargets(ctx, "PUT", reqs)
	if err != nil {
		return err
	}
	commitErrs := make([]error, len(prepared))
	_ = runParallel(len(prepared), 8, func(i int) error {
		_, commitErrs[i] = transfers.CommitTransfer(ctx, envID, prepared[i], keys[i], targets[i])
		return nil
	})
	if closeErr := closeBatch(); closeErr != nil {
		return closeErr
	}
	heads := make([]objectstore.ObjectInfo, len(prepared))
	headErrs := make([]error, len(prepared))
	_ = runParallel(len(prepared), 12, func(i int) error {
		heads[i], headErrs[i] = bucket.Head(ctx, keys[i])
		return nil
	})
	var retryIdx []int
	for i, p := range prepared {
		if headErrs[i] == nil {
			if heads[i].Size != p.SizeBytes {
				return providererr.New(providererr.Transient, "workspace_restore_mismatch", "uploaded blob size mismatch")
			}
			continue
		}
		if commitErrs[i] == nil {
			return headErrs[i]
		}
		if providererr.CategoryOf(headErrs[i]) != providererr.NotFound || !providererr.IsRetryable(commitErrs[i]) {
			return commitErrs[i]
		}
		retryIdx = append(retryIdx, i)
	}
	return runParallel(len(retryIdx), 4, func(j int) error {
		i := retryIdx[j]
		timer := time.NewTimer(200 * time.Millisecond)
		select {
		case <-ctx.Done():
			timer.Stop()
			return ctx.Err()
		case <-timer.C:
		}
		return uploadPreparedBlockWithAttempts(ctx, transfers, envID, bucket, keys[i], prepared[i], opt, 3, 2)
	})
}

// A workspace can contain many small Git objects. A transient CONNECT failure
// must not restart the whole snapshot or re-read mutable source files. Reconcile
// each immutable prepared block first, then retry at most twice when storage
// confirms it is absent. An unknown HEAD result never authorizes another PUT.
func uploadPreparedBlock(ctx context.Context, transfers sandbox.Transfers, envID string, bucket objectstore.Store, key string, prepared sandbox.PreparedTransfer, opt TransferOptions) error {
	return uploadPreparedBlockWithAttempts(ctx, transfers, envID, bucket, key, prepared, opt, 3, 1)
}

func uploadPreparedBlockWithAttempts(ctx context.Context, transfers sandbox.Transfers, envID string, bucket objectstore.Store, key string, prepared sandbox.PreparedTransfer, opt TransferOptions, maxAttempts, startAttempt int) error {
	for attempt := startAttempt; ; attempt++ {
		if err := ctx.Err(); err != nil {
			return err
		}
		signed, err := bucket.SignedUpload(ctx, key, 5*time.Minute, prepared.SHA256, prepared.SizeBytes)
		if err != nil {
			return err
		}
		target, close, err := network(ctx, opt, "PUT", prepared.SizeBytes, sandbox.TransferTarget{URL: signed.URL, Headers: signed.Headers})
		if err != nil {
			return err
		}
		_, uploadErr := transfers.CommitTransfer(ctx, envID, prepared, key, target)
		closeErr := close()
		if closeErr != nil {
			return closeErr
		}
		info, headErr := bucket.Head(ctx, key)
		if headErr == nil {
			// SignedUpload binds this content-addressed key to the exact hash
			// and length and refuses overwrite. HEAD therefore also confirms a
			// completed PUT whose acknowledgement was lost; no body is relayed.
			if info.Size != prepared.SizeBytes {
				return providererr.New(providererr.Transient, "workspace_restore_mismatch", "uploaded blob size mismatch")
			}
			return nil
		}
		if uploadErr == nil {
			return headErr
		}
		if providererr.CategoryOf(headErr) != providererr.NotFound || !providererr.IsRetryable(uploadErr) || attempt >= maxAttempts {
			return uploadErr
		}
		timer := time.NewTimer(time.Duration(attempt) * 200 * time.Millisecond)
		select {
		case <-ctx.Done():
			timer.Stop()
			return ctx.Err()
		case <-timer.C:
		}
	}
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
	// At most 32 URLs are issued at once; every batch gets fresh five-minute signatures.
	for start := 0; start < len(files) || start == 0; start += 32 {
		end := min(start+32, len(files))
		batchFiles := files[start:end]
		batchErr := func() (err error) {
			blocks := make([]sandbox.RestoreBlock, len(batchFiles))
			if len(batchFiles) > 1 && opt.OpenTargets != nil {
				reqs := make([]TransferTargetRequest, len(batchFiles))
				for i, e := range batchFiles {
					ws := workspaceID
					if src, ok := foreign[e.SHA256]; ok {
						ws = src
					}
					u, err := bucket.TemporaryURL(ctx, BlobKey(ws, e.SHA256), 5*time.Minute, "GET")
					if err != nil {
						return err
					}
					reqs[i] = TransferTargetRequest{SizeBytes: e.Size, Target: sandbox.TransferTarget{URL: u}}
				}
				targets, closeBatch, err := opt.OpenTargets(ctx, "GET", reqs)
				if err != nil {
					return err
				}
				defer func() {
					if closeErr := closeBatch(); err == nil {
						err = closeErr
					}
				}()
				for i, e := range batchFiles {
					blocks[i] = sandbox.RestoreBlock{SHA256: e.SHA256, SizeBytes: e.Size, Target: targets[i]}
				}
				return box.RestoreBatch(ctx, envID, id, maxBytes, blocks)
			}
			var cleanups []func() error
			defer func() {
				for _, close := range cleanups {
					if closeErr := close(); err == nil {
						err = closeErr
					}
				}
			}()
			for i, e := range batchFiles {
				ws := workspaceID
				if src, ok := foreign[e.SHA256]; ok {
					ws = src
				}
				u, err := bucket.TemporaryURL(ctx, BlobKey(ws, e.SHA256), 5*time.Minute, "GET")
				if err != nil {
					return err
				}
				target, close, err := network(ctx, opt, "GET", e.Size, sandbox.TransferTarget{URL: u})
				if err != nil {
					return err
				}
				cleanups = append(cleanups, close)
				blocks[i] = sandbox.RestoreBlock{SHA256: e.SHA256, SizeBytes: e.Size, Target: target}
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
