package broker

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"strings"
	"testing"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
)

func requireDiffError(t *testing.T, err error, category providererr.Category, code string) {
	t.Helper()
	var value *providererr.Error
	if !errors.As(err, &value) || value.Category != category || value.Code != code {
		t.Fatalf("want %s/%s, got %v", category, code, err)
	}
}

func TestDiffChunkValidation(t *testing.T) {
	for _, chunk := range []DiffChunk{
		{Size: MaxDiffChunkBytes + 1}, {Offset: 1}, {ExpectedSHA256: strings.Repeat("a", 64)},
		{Size: 1, Offset: 1}, {Size: 1, Offset: MaxFullDiffBytes + 1, ExpectedSHA256: strings.Repeat("a", 64)},
		{Size: 1, ExpectedSHA256: "short"}, {Size: 1, ExpectedSHA256: strings.Repeat("G", 64)},
		{Size: 1, ExpectedSHA256: strings.Repeat("A", 64)},
	} {
		requireDiffError(t, chunk.validate(), providererr.Invalid, "invalid_diff_chunk")
	}
	for _, chunk := range []DiffChunk{{}, {Size: 1}, {Size: MaxDiffChunkBytes, Offset: MaxFullDiffBytes, ExpectedSHA256: strings.Repeat("a", 64)}} {
		if err := chunk.validate(); err != nil {
			t.Fatalf("valid chunk rejected: %v", err)
		}
	}
}

func TestDiffChunksPreserveEveryByteAndDetectChanges(t *testing.T) {
	data := bytes.Repeat([]byte("utf8-边界\xff\x00\n"), 100000)
	sum := sha256.Sum256(data)
	hash := hex.EncodeToString(sum[:])
	var preview DiffResult
	if err := preview.setData(data, DiffChunk{}); err != nil || !preview.Truncated || preview.TotalBytes != uint64(len(data)) || preview.SHA256 != hash || preview.ChunkData != nil {
		t.Fatalf("preview metadata: %v", err)
	}
	var collected []byte
	var offset uint64
	for {
		var result DiffResult
		if err := result.setData(data, DiffChunk{Size: MaxDiffChunkBytes, Offset: offset, ExpectedSHA256: hash}); err != nil {
			t.Fatal(err)
		}
		if result.Diff != "" || result.TotalBytes != uint64(len(data)) || result.SHA256 != hash || len(result.ChunkData) > MaxDiffChunkBytes {
			t.Fatal("chunk metadata or size changed")
		}
		collected = append(collected, result.ChunkData...)
		if !result.Truncated {
			if result.NextOffset != 0 {
				t.Fatal("last chunk must terminate")
			}
			break
		}
		if result.NextOffset <= offset {
			t.Fatal("chunk did not advance")
		}
		offset = result.NextOffset
	}
	if !bytes.Equal(collected, data) {
		t.Fatal("binary/UTF-8 boundaries corrupted the patch")
	}
	var terminal DiffResult
	if err := terminal.setData(data, DiffChunk{Size: 1, Offset: uint64(len(data)), ExpectedSHA256: hash}); err != nil || len(terminal.ChunkData) != 0 || terminal.Truncated || terminal.NextOffset != 0 {
		t.Fatalf("EOF chunk: %v", err)
	}
	var result DiffResult
	requireDiffError(t, result.setData(data, DiffChunk{Size: 1, Offset: uint64(len(data) + 1), ExpectedSHA256: hash}), providererr.Invalid, "invalid_diff_chunk")
	requireDiffError(t, result.setData(append(data, 'x'), DiffChunk{Size: 1, Offset: offset, ExpectedSHA256: hash}), providererr.Conflict, "diff_changed")
}

func TestDiffChunkTotalLimitAndEmptyPatch(t *testing.T) {
	data := make([]byte, MaxFullDiffBytes+1)
	var result DiffResult
	requireDiffError(t, result.setData(data, DiffChunk{Size: 1}), providererr.Invalid, "diff_too_large")
	if err := result.setData(data[:MaxFullDiffBytes], DiffChunk{Size: MaxDiffChunkBytes}); err != nil || result.TotalBytes != MaxFullDiffBytes {
		t.Fatalf("exactly 32 MiB must be supported: %v", err)
	}
	var empty DiffResult
	if err := empty.setData(nil, DiffChunk{Size: 1}); err != nil || empty.Truncated || empty.NextOffset != 0 || empty.TotalBytes != 0 || empty.SHA256 != "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855" {
		t.Fatalf("empty patch: %v", err)
	}
}
