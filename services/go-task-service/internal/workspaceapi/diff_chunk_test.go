package workspaceapi

import (
	"bytes"
	"context"
	"encoding/base64"
	"strings"
	"testing"

	"connectrpc.com/connect"
	"google.golang.org/protobuf/types/known/structpb"

	sandboxv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/sandbox/v1"
	workflowv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workflow/v1"
	workspacev1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workspace/v1"
)

func TestDiffChunksReachSandboxAndBypassGenerationCache(t *testing.T) {
	h, reader := readerHarness(t)
	h.setGeneration(t, "t1", "w1", 1)
	hash := strings.Repeat("a", 64)
	data := []byte{0, 255, 1}
	reader.diff = &workspacev1.GetDiffResponse{ChunkData: data, NextOffset: 6, TotalBytes: 20, Sha256: hash, Truncated: true}
	for range 2 {
		response, err := h.client.GetDiff(context.Background(), authed(h, "t1", &workspacev1.GetDiffRequest{
			WorkspaceId: "w1", Base: "main", FindRenames: true, ChunkSize: 3, ChunkOffset: 3, ExpectedSha256: hash,
		}))
		if err != nil {
			t.Fatal(err)
		}
		got := response.Msg
		if !bytes.Equal(got.GetChunkData(), data) || got.GetNextOffset() != 6 || got.GetTotalBytes() != 20 || got.GetSha256() != hash || !got.GetTruncated() || got.GetDiff() != "" {
			t.Fatal("chunk response not preserved")
		}
	}
	if reader.count() != 2 || h.starts() != 0 {
		t.Fatal("chunks must use live sandbox reads, without a workflow or cached bytes")
	}
	reader.mu.Lock()
	defer reader.mu.Unlock()
	for _, seen := range reader.seen {
		request := seen.(*sandboxv1.GetDiffRequest)
		if request.GetTenant().GetTenantId() != "t1" || request.GetChunkSize() != 3 || request.GetChunkOffset() != 3 || request.GetExpectedSha256() != hash || request.GetBase() != "main" || !request.GetFindRenames() {
			t.Fatal("chunk request or authenticated tenant not preserved")
		}
	}
}

func TestDiffChunksReachWorkerFallback(t *testing.T) {
	h := newHarness(t)
	hash := strings.Repeat("b", 64)
	data := []byte{255, 0, 3}
	h.w.handle = func(input *workflowv1.WorkspaceOperationInput) (*workflowv1.WorkspaceOperationResult, error) {
		args := input.GetArgs().AsMap()
		if args["chunk_size"] != float64(3) || args["chunk_offset"] != float64(7) || args["expected_sha256"] != hash || args["base"] != "main" {
			t.Fatal("chunk arguments not preserved in the worker fallback")
		}
		result, _ := structpb.NewStruct(map[string]any{"chunk_data": base64.StdEncoding.EncodeToString(data), "next_offset": "0", "total_bytes": "10", "sha256": hash})
		return &workflowv1.WorkspaceOperationResult{Result: result}, nil
	}
	response, err := h.client.GetDiff(context.Background(), authed(h, "t1", &workspacev1.GetDiffRequest{WorkspaceId: "w1", Base: "main", ChunkSize: 3, ChunkOffset: 7, ExpectedSha256: hash}))
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(response.Msg.GetChunkData(), data) || response.Msg.GetTotalBytes() != 10 || response.Msg.GetSha256() != hash || response.Msg.GetNextOffset() != 0 || response.Msg.GetDiff() != "" {
		t.Fatal("worker response not preserved")
	}
}

func TestInvalidDiffChunksNeverReachTheExecutor(t *testing.T) {
	h, reader := readerHarness(t)
	for _, request := range []*workspacev1.GetDiffRequest{
		{ChunkSize: 256<<10 + 1}, {ChunkOffset: 1}, {ExpectedSha256: strings.Repeat("a", 64)},
		{ChunkSize: 1, ChunkOffset: 1}, {ChunkSize: 1, ChunkOffset: 32<<20 + 1, ExpectedSha256: strings.Repeat("a", 64)},
		{ChunkSize: 1, ExpectedSha256: "bad"}, {ChunkSize: 1, ExpectedSha256: strings.Repeat("A", 64)},
	} {
		request.WorkspaceId = "w1"
		_, err := h.client.GetDiff(context.Background(), authed(h, "t1", request))
		if code(t, err) != connect.CodeInvalidArgument {
			t.Fatalf("invalid chunk: %v", err)
		}
	}
	if reader.count() != 0 || h.starts() != 0 {
		t.Fatal("invalid requests reached an executor")
	}
}
