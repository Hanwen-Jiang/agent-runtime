package workspaceapi

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"strings"
	"time"

	"connectrpc.com/connect"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
	"google.golang.org/protobuf/reflect/protoregistry"
	"google.golang.org/protobuf/types/known/structpb"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/durable"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/ingress"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	workflowv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workflow/v1"
	workspacev1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workspace/v1"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/taskapi"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/workspaceready"
)

// This index owns idempotency and public result retention; Temporal alone owns execution.
func Migrations() []relational.Migration {
	return []relational.Migration{{Version: 212, Name: "workspace_async_operations", Statements: []string{
		`CREATE TABLE workspace_operations (operation_id TEXT PRIMARY KEY, requested_by TEXT NOT NULL,
    workspace_id TEXT NOT NULL, input_digest TEXT NOT NULL, response_type TEXT NOT NULL, result TEXT NOT NULL DEFAULT '')`,
	}}}
}

type asyncKey struct{}
type asyncStart struct{ requestID, digest, operationID string }

func hashOperation(parts ...string) string {
	h := sha256.New()
	for _, p := range parts {
		h.Write([]byte(p))
		h.Write([]byte{0})
	}
	return hex.EncodeToString(h.Sum(nil))
}

func copyHeader[T any](outer *connect.Request[workspacev1.StartWorkspaceOperationRequest], msg *T) *connect.Request[T] {
	req := connect.NewRequest(msg)
	for k, v := range outer.Header() {
		req.Header()[k] = append([]string(nil), v...)
	}
	return req
}

func (s *Server) StartWorkspaceOperation(ctx context.Context, req *connect.Request[workspacev1.StartWorkspaceOperationRequest]) (*connect.Response[workspacev1.StartWorkspaceOperationResponse], error) {
	m := req.Msg
	if len(m.GetRequestId()) < 1 || len(m.GetRequestId()) > 128 || strings.ContainsAny(m.GetRequestId(), "\x00\r\n") {
		return nil, taskapi.ConnectError(invalid("invalid_request_id", "request_id 必须是 1 到 128 字节的稳定请求标识"))
	}
	raw, err := (proto.MarshalOptions{Deterministic: true}).Marshal(m)
	if err != nil {
		return nil, taskapi.ConnectError(invalid("invalid_operation", "操作参数格式不对"))
	}
	a := &asyncStart{requestID: m.GetRequestId(), digest: hashOperation(string(raw))}
	ctx = context.WithValue(ctx, asyncKey{}, a)
	// Reuse the synchronous handlers, including validation, auth and credential sealing.
	switch op := m.Operation.(type) {
	case *workspacev1.StartWorkspaceOperationRequest_Create:
		_, err = s.CreateWorkspace(ctx, copyHeader(req, op.Create))
	case *workspacev1.StartWorkspaceOperationRequest_Commit:
		_, err = s.CommitWorkspace(ctx, copyHeader(req, op.Commit))
	case *workspacev1.StartWorkspaceOperationRequest_Push:
		_, err = s.PushWorkspace(ctx, copyHeader(req, op.Push))
	case *workspacev1.StartWorkspaceOperationRequest_Archive:
		_, err = s.ArchiveWorkspace(ctx, copyHeader(req, op.Archive))
	case *workspacev1.StartWorkspaceOperationRequest_Delete:
		_, err = s.DeleteWorkspace(ctx, copyHeader(req, op.Delete))
	case *workspacev1.StartWorkspaceOperationRequest_Sync:
		_, err = s.SyncWorkspace(ctx, copyHeader(req, op.Sync))
	case *workspacev1.StartWorkspaceOperationRequest_Discard:
		_, err = s.DiscardChanges(ctx, copyHeader(req, op.Discard))
	case *workspacev1.StartWorkspaceOperationRequest_AttachRepo:
		_, err = s.AttachRepo(ctx, copyHeader(req, op.AttachRepo))
	case *workspacev1.StartWorkspaceOperationRequest_DetachRepo:
		_, err = s.DetachRepo(ctx, copyHeader(req, op.DetachRepo))
	default:
		err = taskapi.ConnectError(invalid("invalid_operation", "要选择一个工作区操作"))
	}
	if err != nil {
		return nil, err
	}
	return connect.NewResponse(&workspacev1.StartWorkspaceOperationResponse{OperationId: a.operationID}), nil
}

func (s *Server) startAsync(ctx context.Context, id ingress.Identity, tenant *commonv1.TenantContext, engine durable.Engine,
	in *workflowv1.WorkspaceOperationInput, execution time.Duration, out proto.Message, a *asyncStart) error {
	db, err := s.d.Store.Bind(tenant)
	if err != nil {
		return err
	}
	return db.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		inserted, err := tx.Execute(ctx, `INSERT INTO workspace_operations (operation_id, requested_by, workspace_id, input_digest, response_type)
   VALUES ($1,$2,$3,$4,$5) ON CONFLICT (operation_id) DO NOTHING`, in.OperationId, id.UserID, in.WorkspaceId, a.digest, string(out.ProtoReflect().Descriptor().FullName()))
		if err != nil {
			return err
		}
		// A write lock serializes retries even if the first request disconnected after StartWorkflow.
		if _, err = tx.Execute(ctx, `UPDATE workspace_operations SET operation_id = $1 WHERE operation_id = $1`, in.OperationId); err != nil {
			return err
		}
		row, _, err := tx.QueryOne(ctx, `SELECT input_digest FROM workspace_operations WHERE operation_id = $1`, in.OperationId)
		if err != nil {
			return err
		}
		if row["input_digest"] != a.digest {
			return providererr.New(providererr.Conflict, "request_id_conflict", "request_id 已被不同的操作参数使用")
		}
		if inserted == 0 {
			// A committed row means this request was accepted, even after Temporal retention.
			a.operationID = in.OperationId
			return nil
		}
		desc, err := engine.Describe(ctx, WorkflowID(in.OperationId))
		if err == nil {
			if desc.WorkflowType != WorkflowName {
				return providererr.New(providererr.Conflict, "operation_type_conflict", "操作编号已被占用")
			}
			a.operationID = in.OperationId
			return nil // Completed workflows must never be started again.
		}
		if providererr.CategoryOf(err) != providererr.NotFound {
			return err
		}
		creating, err := s.markCreating(ctx, tenant, in.Operation, in.WorkspaceId, in.OperationId, execution)
		if err != nil {
			return err
		}
		if _, err = engine.StartWorkflow(ctx, WorkflowName, durable.StartOptions{WorkflowID: WorkflowID(in.OperationId), TaskQueue: TaskQueue, Input: in, ExecutionTimeout: execution}); err != nil {
			// Keep the marker on an ambiguous start; its TTL prevents permanently blocking the workspace.
			return err
		}
		if creating != nil {
			go func(bg context.Context) {
				_, _ = engine.Result(bg, WorkflowID(in.OperationId), &workflowv1.WorkspaceOperationResult{}, execution)
				_ = workspaceready.Clear(bg, creating, in.WorkspaceId, in.OperationId)
			}(context.WithoutCancel(ctx))
		}
		a.operationID = in.OperationId
		return nil
	})
}

func (s *Server) GetWorkspaceOperation(ctx context.Context, req *connect.Request[workspacev1.GetWorkspaceOperationRequest]) (*connect.Response[workspacev1.GetWorkspaceOperationResponse], error) {
	m := req.Msg
	id, tenant, err := s.caller(ctx, req.Header(), "workspace_operation", m.GetTenantId(), m.GetWorkspaceId())
	if err != nil {
		return nil, taskapi.ConnectError(err)
	}
	if m.GetWorkspaceId() == "" || len(m.GetOperationId()) != 68 || !strings.HasPrefix(m.GetOperationId(), "wso-") {
		return nil, taskapi.ConnectError(invalid("invalid_operation_id", "操作编号或工作区不合法"))
	}
	db, err := s.d.Store.Bind(tenant)
	if err != nil {
		return nil, taskapi.ConnectError(err)
	}
	var row relational.Row
	err = db.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		var ok bool
		row, ok, err = tx.QueryOne(ctx, `SELECT requested_by, workspace_id, response_type, result FROM workspace_operations WHERE operation_id = $1`, m.GetOperationId())
		if err != nil {
			return err
		}
		if !ok || row["requested_by"] != id.UserID || row["workspace_id"] != m.GetWorkspaceId() {
			return providererr.New(providererr.NotFound, "operation_not_found", "没有这个工作区操作")
		}
		return nil
	})
	if err != nil {
		return nil, taskapi.ConnectError(err)
	}
	out := &workspacev1.GetWorkspaceOperationResponse{OperationId: m.GetOperationId(), Status: workspacev1.WorkspaceOperationStatus_WORKSPACE_OPERATION_STATUS_RUNNING}
	if saved, _ := row["result"].(string); saved != "" {
		if err = protojson.Unmarshal([]byte(saved), out); err != nil {
			return nil, taskapi.ConnectError(err)
		}
		return connect.NewResponse(out), nil
	}
	engine, err := s.d.Durable.Bind(tenant)
	if err != nil {
		return nil, taskapi.ConnectError(err)
	}
	desc, err := engine.Describe(ctx, WorkflowID(m.GetOperationId()))
	if providererr.CategoryOf(err) == providererr.NotFound {
		// Admission is committed only after StartWorkflow confirms success (or
		// Describe confirms a previous execution). An unconfirmed/rolled-back
		// start cannot reach this branch because its row is not visible above.
		// Whole-workflow timeouts can prevent the final result activity from
		// running; after retention, report unknown side effects explicitly.
		out.Status = workspacev1.WorkspaceOperationStatus_WORKSPACE_OPERATION_STATUS_FAILED
		out.Error = &commonv1.Error{Category: commonv1.ErrorCategory_ERROR_CATEGORY_TRANSIENT,
			Code: "workspace_operation_history_unavailable", Message: "操作已受理，但执行历史与结果已不可用；副作用未知，请核对工作区，不要自动重做"}
		return persistWorkspaceOperation(ctx, db, out)
	}
	if err != nil {
		return nil, taskapi.ConnectError(err)
	}
	if desc.Status == durable.StatusRunning {
		return connect.NewResponse(out), nil
	}
	result := &workflowv1.WorkspaceOperationResult{}
	outcome, err := engine.Result(ctx, WorkflowID(m.GetOperationId()), result, 5*time.Second)
	if err != nil {
		return nil, taskapi.ConnectError(err)
	}
	if outcome.Status != durable.StatusCompleted {
		out.Status = workspacev1.WorkspaceOperationStatus_WORKSPACE_OPERATION_STATUS_FAILED
		out.Error = &commonv1.Error{Category: commonv1.ErrorCategory_ERROR_CATEGORY_TRANSIENT, Code: "workspace_operation_failed", Message: "工作区操作未能完成"}
	} else if e := result.GetError(); e != nil && e.Category != commonv1.ErrorCategory_ERROR_CATEGORY_UNSPECIFIED {
		out.Status = workspacev1.WorkspaceOperationStatus_WORKSPACE_OPERATION_STATUS_FAILED
		out.Error = e
	} else {
		// Deserialize through the exact public response type to discard internal/unknown fields.
		name, _ := row["response_type"].(string)
		typ, e := protoregistry.GlobalTypes.FindMessageByName(protoreflect.FullName(name))
		if e != nil {
			return nil, taskapi.ConnectError(e)
		}
		public := typ.New().Interface()
		data, _ := protojson.Marshal(result.GetResult())
		if e = (protojson.UnmarshalOptions{DiscardUnknown: true}).Unmarshal(data, public); e != nil {
			return nil, taskapi.ConnectError(e)
		}
		data, _ = protojson.Marshal(public)
		out.Result = &structpb.Struct{}
		if e = protojson.Unmarshal(data, out.Result); e != nil {
			return nil, taskapi.ConnectError(e)
		}
		out.Status = workspacev1.WorkspaceOperationStatus_WORKSPACE_OPERATION_STATUS_COMPLETED
	}
	return persistWorkspaceOperation(ctx, db, out)
}

func persistWorkspaceOperation(ctx context.Context, db relational.Store, out *workspacev1.GetWorkspaceOperationResponse) (*connect.Response[workspacev1.GetWorkspaceOperationResponse], error) {
	saved, err := protojson.Marshal(out)
	if err != nil {
		return nil, taskapi.ConnectError(err)
	}
	err = db.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		// A concurrently completing activity has stronger evidence than this
		// observation. Keep whichever terminal result was durably saved first.
		if _, e := tx.Execute(ctx, `UPDATE workspace_operations SET result = $1 WHERE operation_id = $2 AND result = ''`, string(saved), out.GetOperationId()); e != nil {
			return e
		}
		row, _, e := tx.QueryOne(ctx, `SELECT result FROM workspace_operations WHERE operation_id = $1`, out.GetOperationId())
		if e != nil {
			return e
		}
		return protojson.Unmarshal([]byte(row["result"].(string)), out)
	})
	if err != nil {
		return nil, taskapi.ConnectError(err)
	}
	return connect.NewResponse(out), nil
}
