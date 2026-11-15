package taskapi

// D-16 验收 7：提交时的 network_request 按租户策略的 sandbox.network 核对；超出范围的申请被拒并说明原因，被拒的请求不留
// Task、不投递。批准的申请写成这个 Task 的联网授权（sandbox_grants），Task 结束时由 FinishTask 收回。

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	"connectrpc.com/connect"

	"github.com/agentruntime/agent-runtime/libs/go-core/dev/memcache"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	taskapiv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/taskapi/v1"
	workflowv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workflow/v1"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/sandboxgrants"
	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/session"
)

// grantsTable 与 Worker 的迁移 202、203 里的 sandbox_grants 一致（表由 Worker 建）。
var grantsTable = []relational.Migration{{Version: 202, Name: "sandbox_grants", Statements: []string{
	`CREATE TABLE sandbox_grants (
		grant_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, workspace_id TEXT NOT NULL DEFAULT '', session_id TEXT NOT NULL DEFAULT '',
		scope TEXT NOT NULL DEFAULT 'hosts', hosts TEXT NOT NULL, ports TEXT NOT NULL, expires_at TEXT NOT NULL, max_up_bytes BIGINT NOT NULL DEFAULT 0,
		max_down_bytes BIGINT NOT NULL DEFAULT 0, max_conns BIGINT NOT NULL DEFAULT 0, max_rps BIGINT NOT NULL DEFAULT 0,
		up_bytes BIGINT NOT NULL DEFAULT 0, down_bytes BIGINT NOT NULL DEFAULT 0, preview_port BIGINT NOT NULL DEFAULT 0,
		state TEXT NOT NULL, approved_by TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
		revoked_at TEXT NOT NULL DEFAULT '', preview_env TEXT NOT NULL DEFAULT '', preview_target TEXT NOT NULL DEFAULT '')`,
}}}

const networkPolicies = `{"tenants": {
  "tu": {"effect_ceiling": "write_reversible", "sandbox": {"network": {"allowed": true, "allow_unlimited": true, "modes": ["deny", "allowlist", "open"], "max_grant_minutes": 60}}},
  "tp": {"effect_ceiling": "write_irreversible", "sandbox": {"network": {"allowed": true, "modes": ["deny", "open"], "max_conns": 4}}},
  "tn": {"effect_ceiling": "write_irreversible", "sandbox": {"network": {"allowed": true, "hosts": ["api.example.com", "*.pypi.org"],
         "max_grant_minutes": 30, "max_up_bytes": 1048576, "max_conns": 4}}},
  "ta": {"effect_ceiling": "write_irreversible", "sandbox": {"network": {"allowed": true, "hosts": ["api.example.com"], "approver": "admin"}}},
  "tr": {"effect_ceiling": "read", "sandbox": {"network": {"allowed": true, "hosts": ["api.example.com"]}}},
  "tx": {"effect_ceiling": "write_irreversible"}
}}`

func networkAPI(t *testing.T) *apiFixture {
	t.Helper()
	path := writePolicies(t, networkPolicies)
	f := newAPIWith(t, func(d *Deps) { d.Policies = FilePolicyResolver{Path: path, Fallback: d.Defaults} })
	if err := f.store.Migrate(context.Background(), grantsTable); err != nil {
		t.Fatal(err)
	}
	return f
}

func netRequest(crid string, hosts []string, minutes int32) *taskapiv1.SubmitTaskRequest {
	req := newRequest(crid)
	req.NetworkRequest = &taskapiv1.NetworkRequest{Hosts: hosts, Minutes: minutes, Reason: "拉取 API 文档"}
	return req
}

func (f *apiFixture) grantRows(tenant string) []relational.Row {
	f.t.Helper()
	db, err := f.store.Bind(&commonv1.TenantContext{TenantId: tenant})
	if err != nil {
		f.t.Fatal(err)
	}
	var rows []relational.Row
	if err := db.Transaction(context.Background(), relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		var err error
		rows, err = tx.Query(ctx, "SELECT grant_id, task_id, scope, hosts, ports, expires_at, state, approved_by, reason, max_up_bytes, max_conns, created_at FROM sandbox_grants ORDER BY created_at")
		return err
	}); err != nil {
		f.t.Fatal(err)
	}
	return rows
}

func TestSubmitNetworkRequestBecomesTheTasksGrant(t *testing.T) {
	f := networkAPI(t)
	resp := f.mustSubmit(f.token("tn", "u1"), netRequest("c-net", []string{"API.example.com", "files.pypi.org"}, 15))
	rows := f.grantRows("tn")
	if len(rows) != 1 {
		t.Fatalf("授权：%v", rows)
	}
	g := rows[0]
	var hosts []string
	_ = json.Unmarshal([]byte(g["hosts"].(string)), &hosts)
	if g["task_id"] != resp.GetTaskId() || g["state"] != "active" || g["approved_by"] != "u1" || g["reason"] != "拉取 API 文档" ||
		strings.Join(hosts, ",") != "api.example.com,files.pypi.org" || g["ports"] != "[80,443]" ||
		g["max_up_bytes"].(int64) != 1048576 || g["max_conns"].(int64) != 4 || g["grant_id"] != sandboxgrants.SubmitGrantID(resp.GetTaskId()) {
		t.Fatalf("授权的内容：%v", g)
	}
	created, _ := time.Parse("2006-01-02T15:04:05.000000Z", g["created_at"].(string))
	expires, _ := time.Parse("2006-01-02T15:04:05.000000Z", g["expires_at"].(string))
	if expires.Sub(created) != 15*time.Minute {
		t.Errorf("15 分钟的授权，实际 %v", expires.Sub(created))
	}
	// 同一个 client_request_id 重复提交：同一个 Task，不多写一份授权。
	again := f.mustSubmit(f.token("tn", "u1"), netRequest("c-net", []string{"api.example.com"}, 15))
	if again.GetTaskId() != resp.GetTaskId() || len(f.grantRows("tn")) != 1 {
		t.Fatalf("重复提交：%v，授权 %d 条", again, len(f.grantRows("tn")))
	}

	// Task 结束：FinishTask 收回它的授权。
	acts := &session.Activities{Cache: memcache.New(memcache.Options{Clock: f.clock}), Store: f.store, Clock: f.clock, Retention: time.Hour}
	tenant := &commonv1.TenantContext{TenantId: "tn", UserId: "u1", SessionId: resp.GetSessionId()}
	if err := acts.FinishTask(context.Background(), tenant, &workflowv1.TaskWorkflowResult{TaskId: resp.GetTaskId(), Status: commonv1.TaskStatus_TASK_STATUS_DONE}); err != nil {
		t.Fatal(err)
	}
	if rows := f.grantRows("tn"); rows[0]["state"] != "revoked" {
		t.Fatalf("Task 结束之后授权还在：%v", rows[0])
	}
}

func TestSubmitNetworkRequestOutsidePolicyIsRefusedWithReason(t *testing.T) {
	f := networkAPI(t)
	badPort := netRequest("c12", []string{"api.example.com"}, 15)
	badPort.NetworkRequest.Ports = []int32{0}
	cases := []struct {
		name, tenant string
		req          *taskapiv1.SubmitTaskRequest
		code         connect.Code
		reason       string // 错误里说明的原因
	}{
		{"不在主机范围", "tn", netRequest("c1", []string{"evil.example.org"}, 15), connect.CodePermissionDenied, "不在租户策略能放行的主机范围里"},
		{"通配比策略宽", "tn", netRequest("c2", []string{"*.example.com"}, 15), connect.CodePermissionDenied, "不在租户策略能放行的主机范围里"},
		{"超过最长时长", "tn", netRequest("c3", []string{"api.example.com"}, 60), connect.CodePermissionDenied, "授权最长 30 分钟"},
		{"不是三档之一", "tn", netRequest("c4", []string{"api.example.com"}, 20), connect.CodeInvalidArgument, "15、30、60 分钟"},
		{"IP 地址", "tn", netRequest("c5", []string{"169.254.169.254"}, 15), connect.CodeInvalidArgument, "不能是 IP 地址"},
		{"内网名字", "tn", netRequest("c6", []string{"db.internal"}, 15), connect.CodePermissionDenied, "内网的名字"},
		{"没写主机", "tn", netRequest("c7", nil, 15), connect.CodeInvalidArgument, "要写明访问哪些主机"},
		{"策略不允许", "tx", netRequest("c8", []string{"api.example.com"}, 15), connect.CodePermissionDenied, "不允许沙箱联网"},
		{"要管理员批", "ta", netRequest("c10", []string{"api.example.com"}, 15), connect.CodePermissionDenied, "要求管理员批准联网"},
		{"端口不对", "tn", badPort, connect.CodeInvalidArgument, "端口 0 不在"},
	}

	before := len(f.durable.callsOf("SignalWithStart"))
	for _, c := range cases {
		_, err := f.submit(f.token(c.tenant, "u1"), c.req)
		wantCode(t, c.name, err, c.code)
		if !strings.Contains(err.Error(), c.reason) {
			t.Errorf("%s：要说明原因（%s），实际 %v", c.name, c.reason, err)
		}
	}
	if after := len(f.durable.callsOf("SignalWithStart")); after != before {
		t.Errorf("被拒的请求不应投递")
	}
	for _, tn := range []string{"tn", "ta", "tr", "tx"} {
		if rows := f.grantRows(tn); len(rows) != 0 {
			t.Errorf("被拒的请求不应留下授权：%s %v", tn, rows)
		}
	}
	// approver = admin：带审批权限的身份提交就是管理员批准。
	admin := f.ingress.IssueTokenWithScopes("ta", "boss", time.Hour, ScopeApprove)
	f.mustSubmit(admin, netRequest("c-admin", []string{"api.example.com"}, 15))
	if rows := f.grantRows("ta"); len(rows) != 1 || rows[0]["approved_by"] != "boss" {
		t.Fatalf("管理员提交的授权：%v", rows)
	}
}

// 表还没建（Worker 还没起来迁移）：提交 transient，客户端稍后重试。
func TestSubmitNetworkRequestBeforeSandboxTablesIsTransient(t *testing.T) {
	path := writePolicies(t, networkPolicies)
	f := newAPIWith(t, func(d *Deps) { d.Policies = FilePolicyResolver{Path: path, Fallback: d.Defaults} })
	_, err := f.submit(f.token("tn", "u1"), netRequest("c-early", []string{"api.example.com"}, 15))
	wantCode(t, "表还没建", err, connect.CodeUnavailable)
}

func TestNetworkPolicyFromRegistryDocument(t *testing.T) {
	p, err := NetworkPolicyFromDocument([]byte(`{"effect_ceiling": "write_irreversible", "sandbox": {"backend": "kata",
		"network": {"allowed": true, "hosts": ["*.pypi.org"], "approver": "admin", "max_rps": 5}}}`))
	if err != nil || !p.Allowed || p.Hosts[0] != "*.pypi.org" || p.approver() != "admin" || p.MaxRPS != 5 || p.maxMinutes() != 60 {
		t.Fatalf("%+v %v", p, err)
	}
	if p, err := NetworkPolicyFromDocument(nil); err != nil || p.Allowed {
		t.Fatalf("没有策略：%+v %v", p, err)
	}
}

func TestSubmitUnlimitedGrantEndsWithItsTask(t *testing.T) {
	f := networkAPI(t)
	req := newRequest("explicit-unlimited")
	req.NetworkRequest = &taskapiv1.NetworkRequest{Scope: taskapiv1.NetworkScope_NETWORK_SCOPE_PUBLIC, Minutes: 0, Unlimited: true, Reason: "long research"}
	result := f.mustSubmit(f.token("tu", "u1"), req)
	rows := f.grantRows("tu")
	if len(rows) != 1 || rows[0]["expires_at"] != sandboxgrants.UnlimitedExpiresAt || rows[0]["task_id"] != result.GetTaskId() {
		t.Fatal(rows)
	}
	acts := &session.Activities{Cache: memcache.New(memcache.Options{Clock: f.clock}), Store: f.store, Clock: f.clock, Retention: time.Hour}
	tenant := &commonv1.TenantContext{TenantId: "tu", UserId: "u1", SessionId: result.GetSessionId()}
	if err := acts.FinishTask(context.Background(), tenant, &workflowv1.TaskWorkflowResult{TaskId: result.GetTaskId(), Status: commonv1.TaskStatus_TASK_STATUS_DONE}); err != nil {
		t.Fatal(err)
	}
	if f.grantRows("tu")[0]["state"] != "revoked" {
		t.Fatal("unlimited grant outlived Task completion")
	}
}

func TestReadOnlyTaskCanExplicitlyAuthorizeNetworkWithoutWriteUpgrade(t *testing.T) {
	for _, tc := range []struct {
		name, tenant string
		requested    commonv1.Effect
	}{
		{"read-only tenant", "tr", commonv1.Effect_EFFECT_UNSPECIFIED},
		{"explicitly lowered task", "tn", commonv1.Effect_EFFECT_READ},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := networkAPI(t)
			req := netRequest("read-network", []string{"api.example.com"}, 15)
			req.EffectCeiling = tc.requested
			result := f.mustSubmit(f.token(tc.tenant, "u1"), req)
			_, signal := f.lastSubmit()
			if signal.GetTask().GetEffectCeiling() != commonv1.Effect_EFFECT_READ {
				t.Fatal("network request upgraded workspace permissions", signal.GetTask())
			}
			rows := f.grantRows(tc.tenant)
			if len(rows) != 1 || rows[0]["task_id"] != result.GetTaskId() || rows[0]["approved_by"] != "u1" {
				t.Fatal("explicit network grant missing", rows)
			}
			plain := newRequest("read-without-network")
			plain.EffectCeiling = commonv1.Effect_EFFECT_READ
			f.mustSubmit(f.token(tc.tenant, "u1"), plain)
			if len(f.grantRows(tc.tenant)) != 1 {
				t.Fatal("network was automatically granted without network_request")
			}
		})
	}
	f := networkAPI(t)
	elevated := netRequest("cannot-upgrade-read", []string{"api.example.com"}, 15)
	elevated.EffectCeiling = commonv1.Effect_EFFECT_WRITE_REVERSIBLE
	_, err := f.submit(f.token("tr", "u1"), elevated)
	wantCode(t, "network authorization must not allow workspace writes", err, connect.CodePermissionDenied)
	if len(f.grantRows("tr")) != 0 || len(f.durable.callsOf("SignalWithStart")) != 0 {
		t.Fatal("rejected write upgrade left a task or grant")
	}
}
