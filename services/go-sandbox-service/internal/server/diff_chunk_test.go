package server_test

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"connectrpc.com/connect"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	sandboxv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/sandbox/v1"
)

func TestDiffChunkExportMatchesCompleteGitPatch(t *testing.T) {
	h := newHarness(t)
	root := h.mkWorkspace("t1", "w1", map[string]string{"large.txt": "original\n"})
	for _, args := range [][]string{{"init", "-q", "-b", "main"}, {"add", "-A"}, {"-c", "user.name=T", "-c", "user.email=t@example.test", "commit", "-qm", "initial"}} {
		if out, err := gitCmd(root, args...).CombinedOutput(); err != nil {
			t.Fatalf("git fixture failed: %v %s", err, out)
		}
	}
	if err := os.WriteFile(filepath.Join(root, "large.txt"), []byte(strings.Repeat("changed-边界\n", 90000)), 0o644); err != nil {
		t.Fatal(err)
	}
	expected, err := gitCmd(root, "diff", "--binary", "--no-color", "--no-ext-diff", "--no-renames", "HEAD", "--").Output()
	if err != nil || len(expected) <= 1<<20 {
		t.Fatalf("large patch fixture: %v, bytes=%d", err, len(expected))
	}
	sum := sha256.Sum256(expected)
	hash := hex.EncodeToString(sum[:])
	ctx := context.Background()
	request := &sandboxv1.GetDiffRequest{Tenant: tenant("t1", "w1", ""), WorkspaceId: "w1"}
	preview, err := h.reader.GetDiff(ctx, connect.NewRequest(request))
	if err != nil {
		t.Fatal(err)
	}
	if got := preview.Msg.GetResponse(); !got.GetTruncated() || got.GetTotalBytes() != uint64(len(expected)) || got.GetSha256() != hash || len(got.GetDiff()) > 1<<20+3 || len(got.GetChunkData()) != 0 {
		t.Fatal("default preview behavior or complete patch metadata changed")
	}
	request.ChunkSize = 256 << 10
	request.ExpectedSha256 = hash
	var collected []byte
	for {
		response, err := h.reader.GetDiff(ctx, connect.NewRequest(request))
		if err != nil {
			t.Fatal(err)
		}
		got := response.Msg.GetResponse()
		if got.GetDiff() != "" || got.GetSha256() != hash || got.GetTotalBytes() != uint64(len(expected)) || len(got.GetChunkData()) > 256<<10 {
			t.Fatal("inconsistent chunk response")
		}
		collected = append(collected, got.GetChunkData()...)
		if got.GetNextOffset() == 0 {
			if got.GetTruncated() {
				t.Fatal("terminal chunk marked truncated")
			}
			break
		}
		if !got.GetTruncated() || got.GetNextOffset() <= request.ChunkOffset {
			t.Fatal("chunk did not advance")
		}
		request.ChunkOffset = got.GetNextOffset()
	}
	if !bytes.Equal(collected, expected) {
		t.Fatal("export differs from the complete real Git patch")
	}
	if err := os.WriteFile(filepath.Join(root, "large.txt"), []byte("changed again\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	_, err = h.reader.GetDiff(ctx, connect.NewRequest(request))
	if category, value := code(t, err); category != providererr.Conflict || value != "diff_changed" {
		t.Fatalf("must reject changed content between pages: %v %s", category, value)
	}
	for _, row := range h.rows("t1", "SELECT state, writable FROM sandbox_envs") {
		if row["state"] != "destroyed" {
			t.Fatalf("chunk collection left an environment alive: %v", row["state"])
		}
	}
}

func TestInvalidDiffChunkRejectedBeforeAcquiringSandbox(t *testing.T) {
	h := newHarness(t)
	_, err := h.reader.GetDiff(context.Background(), connect.NewRequest(&sandboxv1.GetDiffRequest{
		Tenant: tenant("t1", "w1", ""), WorkspaceId: "w1", ChunkSize: 256<<10 + 1,
	}))
	if category, value := code(t, err); category != providererr.Invalid || value != "invalid_diff_chunk" {
		t.Fatalf("invalid chunk: %v %s", category, value)
	}
	if len(h.rows("t1", "SELECT env_ref FROM sandbox_envs")) != 0 {
		t.Fatal("invalid chunk acquired a sandbox")
	}
}
