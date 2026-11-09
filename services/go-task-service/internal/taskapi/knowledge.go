package taskapi

import (
	"connectrpc.com/connect"
	"context"
	"crypto/sha256"
	"crypto/tls"
	"encoding/hex"
	"encoding/json"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	memoryv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/memory/v1"
	"github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/memory/v1/memoryv1connect"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/files"
	"golang.org/x/net/http2"
	"google.golang.org/protobuf/types/known/structpb"
	"net"
	"net/http"
	"os"
	"strings"
	"time"
)

type Knowledge interface {
	Execute(context.Context, *commonv1.TenantContext, string, string, map[string]any) (map[string]any, error)
}
type knowledgeClient struct {
	client memoryv1connect.KnowledgeServiceClient
	token  string
}

func newKnowledge() Knowledge {
	addr := os.Getenv("AR_MEMORY_ADDRESS")
	token := os.Getenv("AR_KNOWLEDGE_INTERNAL_TOKEN")
	if addr == "" || token == "" {
		return nil
	}
	if !strings.Contains(addr, "://") {
		addr = "http://" + addr
	}
	transport := &http2.Transport{}
	if strings.HasPrefix(addr, "http://") {
		transport.AllowHTTP = true
		transport.DialTLSContext = func(ctx context.Context, network, addr string, _ *tls.Config) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, network, addr)
		}
	}
	return &knowledgeClient{client: memoryv1connect.NewKnowledgeServiceClient(&http.Client{Transport: transport, Timeout: 5 * time.Minute}, addr, connect.WithGRPC(), connect.WithSendMaxBytes(24<<20), connect.WithReadMaxBytes(16<<20)), token: token}
}
func (k *knowledgeClient) Execute(ctx context.Context, t *commonv1.TenantContext, bearer, operation string, payload map[string]any) (map[string]any, error) {
	v, e := structpb.NewValue(payload)
	if e != nil {
		return nil, e
	}
	req := connect.NewRequest(&memoryv1.KnowledgeServiceExecuteRequest{Tenant: t, Operation: operation, Payload: &commonv1.JsonPayload{SchemaId: "knowledge_operation_v1", Value: v}})
	req.Header().Set("Authorization", knowledgeAssertion(t, bearer))
	req.Header().Set("X-Ar-Knowledge-Token", k.token)
	out, e := k.client.Execute(ctx, req)
	if e != nil {
		return nil, e
	}
	result, ok := out.Msg.GetResult().GetValue().AsInterface().(map[string]any)
	if !ok {
		return nil, invalid("invalid retrieval response")
	}
	return result, nil
}
func collectionID(actor, workspace string) string {
	sum := sha256.Sum256([]byte(actor + "\x00" + workspace))
	return "col_" + hex.EncodeToString(sum[:])[:32]
}
func (s *Server) fileIndex(w http.ResponseWriter, r *http.Request) {
	id, t, _, e := s.uploadObjects(r)
	if e != nil {
		fileError(w, e)
		return
	}
	if s.d.Knowledge == nil {
		fileError(w, providererr.New(providererr.Transient, "knowledge_unconfigured", "Knowledge Service is not configured"))
		return
	}
	var body struct {
		JobIDs           []string `json:"job_ids"`
		ExpectedRevision int64    `json:"expected_revision"`
	}
	if e = readFileBody(w, r, &body); e != nil {
		fileError(w, e)
		return
	}
	if len(body.JobIDs) < 1 || len(body.JobIDs) > 500 {
		fileError(w, invalid("select 1 to 500 parsed jobs"))
		return
	}
	docs := []any{}
	roots := []files.Root{}
	seen := map[string]bool{}
	seenRepresentations := map[string]bool{}
	coverage := []any{}
	for _, jid := range body.JobIDs {
		j, e := s.files.Job(r.Context(), t, id.UserID, jid)
		if e != nil {
			fileError(w, e)
			return
		}
		if j.Status != "completed" && j.Status != "partial" || len(j.Inputs) != 1 || (j.Operation != "read" && j.Operation != "index") {
			fileError(w, invalid("index requires parsed immutable documents"))
			return
		}
		root := j.Inputs[0]
		representation, _ := j.Result["representation_id"].(string)
		if seenRepresentations[representation] {
			continue
		}
		seenRepresentations[representation] = true
		if !seen[root.VersionID] {
			roots = append(roots, root)
			seen[root.VersionID] = true
		}
		coverage = append(coverage, map[string]any{"version_id": root.VersionID, "representation_id": representation, "coverage": j.Result["coverage"], "status": j.Status})
		docs = append(docs, map[string]any{"version_id": root.VersionID, "representation_id": j.Result["representation_id"], "blocks": j.Result["blocks"]})
	}
	payloadBytes, _ := json.Marshal(docs)
	if len(payloadBytes) > 20<<20 {
		fileError(w, invalid("selected representations exceed 20 MiB index payload; choose smaller ranges"))
		return
	}
	colID := collectionID(id.UserID, t.GetWorkspaceId())
	raw, _ := json.Marshal([]any{docs, os.Getenv("AR_EMBEDDING_VERSION"), body.ExpectedRevision})
	hash := sha256.Sum256(raw)
	generation := "gen_" + hex.EncodeToString(hash[:])[:40]
	result, e := s.d.Knowledge.Execute(r.Context(), t, r.Header.Get("Authorization"), "index", map[string]any{"collection_id": colID, "generation": generation, "documents": docs})
	if e != nil {
		fileError(w, e)
		return
	}
	result["source_coverage"] = coverage
	embedding, _ := result["embedding_version"].(string)
	if embedding == "" {
		fileError(w, invalid("missing embedding version"))
		return
	}
	col, e := s.files.PublishCollection(r.Context(), t, id.UserID, files.Collection{ID: colID, Generation: generation, EmbeddingVersion: embedding, Roots: roots}, body.ExpectedRevision)
	if e != nil {
		fileError(w, e)
		return
	}
	for i := range col.Roots {
		col.Roots[i].ObjectURI = ""
	}
	fileJSON(w, 200, map[string]any{"collection": col, "index": result})
}
func (s *Server) fileSearch(w http.ResponseWriter, r *http.Request) {
	id, t, _, e := s.uploadObjects(r)
	if e != nil {
		fileError(w, e)
		return
	}
	if s.d.Knowledge == nil {
		fileError(w, providererr.New(providererr.Transient, "knowledge_unconfigured", "Knowledge Service is not configured"))
		return
	}
	var body struct {
		Query   string   `json:"query"`
		TaskID  string   `json:"task_id"`
		Limit   int      `json:"limit"`
		NodeIDs []string `json:"node_ids"`
	}
	if e = readFileBody(w, r, &body); e != nil {
		fileError(w, e)
		return
	}
	col, e := s.files.Collection(r.Context(), t, id.UserID, collectionID(id.UserID, t.GetWorkspaceId()), body.TaskID)
	if e != nil {
		fileError(w, e)
		return
	}
	operation := "search"
	if body.Limit == 0 {
		body.Limit = 8
	}
	payload := map[string]any{"collection_id": col.ID, "generation": col.Generation, "embedding_version": col.EmbeddingVersion, "query": body.Query, "limit": body.Limit}
	if len(body.NodeIDs) > 0 {
		operation = "resolve"
		payload = map[string]any{"collection_id": col.ID, "generation": col.Generation, "node_ids": body.NodeIDs}
	}
	result, e := s.d.Knowledge.Execute(r.Context(), t, r.Header.Get("Authorization"), operation, payload)
	if e != nil {
		fileError(w, e)
		return
	}
	after, e := s.files.Collection(r.Context(), t, id.UserID, col.ID, body.TaskID)
	if e != nil {
		fileError(w, e)
		return
	}
	if after.Generation != col.Generation {
		fileError(w, providererr.New(providererr.Conflict, "index_changed", "index changed during query; retry"))
		return
	}
	fileJSON(w, 200, result)
}
func (s *Server) fileCollection(w http.ResponseWriter, r *http.Request) {
	id, t, _, e := s.uploadObjects(r)
	if e != nil {
		fileError(w, e)
		return
	}
	col, e := s.files.Collection(r.Context(), t, id.UserID, collectionID(id.UserID, t.GetWorkspaceId()), "")
	if e != nil {
		fileError(w, e)
		return
	}
	for i := range col.Roots {
		col.Roots[i].ObjectURI = ""
	}
	fileJSON(w, 200, col)
}

func requestedFileCollections(inputs *structpb.Struct) ([]string, bool, error) {
	raw, ok := inputs.GetFields()["file_collection_ids"]
	if !ok {
		return nil, false, nil
	}
	list := raw.GetListValue()
	if list == nil || len(list.Values) > 1 {
		return nil, true, invalid("file_collection_ids must select at most one current workspace collection")
	}
	ids := []string{}
	for _, value := range list.Values {
		id := value.GetStringValue()
		if len(id) != 36 || !strings.HasPrefix(id, "col_") {
			return nil, true, invalid("invalid file collection identity")
		}
		if _, e := hex.DecodeString(id[4:]); e != nil {
			return nil, true, invalid("invalid file collection identity")
		}
		ids = append(ids, id)
	}
	return ids, true, nil
}
