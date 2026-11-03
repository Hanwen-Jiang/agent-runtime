package workspaceapi

// ENV-02（人工验收 Q16–37；docs/plan/env-02-implementation-2026-11-03.md）：环境配置随建工作区到 Runtime，删除环境配置只标记、
// 由对账异步清理。

import (
	"context"
	"strings"
	"testing"

	"connectrpc.com/connect"
	"google.golang.org/protobuf/encoding/protojson"

	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	workflowv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workflow/v1"
	workspacev1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workspace/v1"
)

func sampleEnvironment() *workspacev1.WorkspaceEnvironment {
	return &workspacev1.WorkspaceEnvironment{
		EnvironmentId: "env-py", SetupScript: "pip install uv\n", StartCommand: "nohup redis-server >/dev/null 2>&1 &\n",
		Env: "GREETING=hello\nMODE=a=b\n",
		Network: &workspacev1.EnvironmentNetwork{Mode: workspacev1.NetworkMode_NETWORK_MODE_RESTRICTED, Hosts: []*workspacev1.NetworkHost{
			{Host: "pypi.org"}, {Host: "github.com", IncludeSubdomains: true},
		}},
		SecretHeaders: []*workspacev1.SecretHeader{{Host: "api.example.com", SecretName: "example-token"}, {Host: "x.example.com", SecretName: "x", Header: "x-api-key"}},
	}
}

func TestCreateCarriesTheEnvironmentSnapshotToTheWorker(t *testing.T) {
	h := newHarness(t)
	_, err := h.client.CreateWorkspace(context.Background(), authed(h, "t1", &workspacev1.CreateWorkspaceRequest{
		TenantId: "t1", WorkspaceId: "w1", FromWorkspaceId: "base", Environment: sampleEnvironment(),
	}))
	if err != nil {
		t.Fatal(err)
	}
	args := h.w.started[0].GetArgs().AsMap()
	env, ok := args["environment"].(map[string]any)
	if !ok {
		t.Fatalf("工作流的参数里要有 environment：%v", args)
	}
	if env["environment_id"] != "env-py" || env["setup_script"] != "pip install uv\n" || env["env"] != "GREETING=hello\nMODE=a=b\n" {
		t.Fatalf("快照不对：%v", env)
	}
	network := env["network"].(map[string]any)
	if network["mode"] != "NETWORK_MODE_RESTRICTED" || len(network["hosts"].([]any)) != 2 {
		t.Fatalf("联网配置不对：%v", network)
	}
	headers := env["secret_headers"].([]any)
	if headers[0].(map[string]any)["secret_name"] != "example-token" {
		t.Fatalf("网络密钥只带名字：%v", headers)
	}
	// 没给 environment 时参数里没有它（老客户端的行为不变）。
	_, _ = h.client.CreateWorkspace(context.Background(), authed(h, "t1", &workspacev1.CreateWorkspaceRequest{TenantId: "t1", WorkspaceId: "w2"}))
	if _, ok := h.w.started[1].GetArgs().AsMap()["environment"]; ok {
		t.Fatal("没选用环境时不传 environment")
	}
}

func TestEnvironmentIsCheckedBeforeStartingAnything(t *testing.T) {
	h := newHarness(t)
	big := strings.Repeat("x", 64<<10+1)
	cases := map[string]func(e *workspacev1.WorkspaceEnvironment){
		"bad id":           func(e *workspacev1.WorkspaceEnvironment) { e.EnvironmentId = "bad id" },
		"empty id":         func(e *workspacev1.WorkspaceEnvironment) { e.EnvironmentId = "" },
		"big setup":        func(e *workspacev1.WorkspaceEnvironment) { e.SetupScript = big },
		"big start":        func(e *workspacev1.WorkspaceEnvironment) { e.StartCommand = big },
		"bad env line":     func(e *workspacev1.WorkspaceEnvironment) { e.Env = "no equals" },
		"secret in env":    func(e *workspacev1.WorkspaceEnvironment) { e.Env = "GITHUB_TOKEN=ghp_SECRET" },
		"reserved env":     func(e *workspacev1.WorkspaceEnvironment) { e.Env = "HTTPS_PROXY=http://evil" },
		"ip host":          func(e *workspacev1.WorkspaceEnvironment) { e.Network.Hosts[0].Host = "10.0.0.1" },
		"star host":        func(e *workspacev1.WorkspaceEnvironment) { e.Network.Hosts[0].Host = "*" },
		"secret path":      func(e *workspacev1.WorkspaceEnvironment) { e.SecretHeaders[0].SecretName = "../u2/token" },
		"host header":      func(e *workspacev1.WorkspaceEnvironment) { e.SecretHeaders[0].Header = "Host" },
		"proxy header":     func(e *workspacev1.WorkspaceEnvironment) { e.SecretHeaders[0].Header = "Proxy-Authorization" },
		"secret host":      func(e *workspacev1.WorkspaceEnvironment) { e.SecretHeaders[0].Host = "localhost" },
		"too many headers": func(e *workspacev1.WorkspaceEnvironment) { e.SecretHeaders = append(e.SecretHeaders, make([]*workspacev1.SecretHeader, 20)...) },
	}
	for name, mutate := range cases {
		env := sampleEnvironment()
		mutate(env)
		_, err := h.client.CreateWorkspace(context.Background(), authed(h, "t1", &workspacev1.CreateWorkspaceRequest{TenantId: "t1", WorkspaceId: "w1", Environment: env}))
		if code(t, err) != connect.CodeInvalidArgument {
			t.Fatalf("%s：要 InvalidArgument，得到 %v", name, err)
		}
		if strings.Contains(err.Error(), "ghp_SECRET") {
			t.Fatalf("%s：错误里不能带值", name)
		}
	}
	if len(h.w.started) != 0 {
		t.Fatal("参数不对时不起工作流")
	}
}

func workspaceTable(t *testing.T, h *harness) relational.Store {
	t.Helper()
	ctx := context.Background()
	if err := h.store.Migrate(ctx, []relational.Migration{{Version: 205, Name: "workspaces", Statements: []string{
		`CREATE TABLE workspaces (workspace_id TEXT PRIMARY KEY, state TEXT NOT NULL, repo_url TEXT NOT NULL DEFAULT '', head TEXT NOT NULL DEFAULT '',
			snapshot_id TEXT NOT NULL DEFAULT '', size_bytes BIGINT NOT NULL DEFAULT 0, files BIGINT NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
			updated_at TEXT NOT NULL, deleted_at TEXT NOT NULL DEFAULT '')`,
	}}, {Version: 207, Name: "workspace_environment", Statements: []string{
		`ALTER TABLE workspaces ADD COLUMN environment_id TEXT NOT NULL DEFAULT ''`,
		`ALTER TABLE workspaces ADD COLUMN environment TEXT NOT NULL DEFAULT ''`,
		`ALTER TABLE workspaces ADD COLUMN environment_owner TEXT NOT NULL DEFAULT ''`,
		`ALTER TABLE workspaces ADD COLUMN environment_deleted_at TEXT NOT NULL DEFAULT ''`,
		`ALTER TABLE workspaces ADD COLUMN environment_secrets TEXT NOT NULL DEFAULT ''`,
	}}}); err != nil {
		t.Fatal(err)
	}
	db, _ := h.store.Bind(&commonv1.TenantContext{TenantId: "t1"})
	if err := db.Transaction(ctx, relational.TxOptions{}, func(ctx context.Context, tx relational.Tx) error {
		for _, r := range [][]any{{"w1", "env-py", "u-alice"}, {"w2", "env-py", "u-alice"}, {"w3", "env-py", "u-bob"}, {"w4", "env-other", "u-alice"}} {
			if _, err := tx.Execute(ctx, `INSERT INTO workspaces (workspace_id, state, created_at, updated_at, environment_id, environment, environment_owner,
				environment_secrets) VALUES ($1, 'active', '2026-11-03T00:00:00.000000Z', '2026-11-03T00:00:00.000000Z', $2, '{"setup_script":"x"}', $3,
				'{"env":"A=1"}')`, r...); err != nil {
				return err
			}
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	return db
}

func TestDeleteEnvironmentConfigMarksOnlyTheCallersBindings(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	// 表还没有（Worker 还没迁移）：没有绑定，删掉 0 个。
	resp, err := h.client.DeleteEnvironmentConfig(ctx, authed(h, "t1", &workspacev1.DeleteEnvironmentConfigRequest{TenantId: "t1", EnvironmentId: "env-py"}))
	if err != nil || resp.Msg.GetWorkspaces() != 0 {
		t.Fatalf("没有表时为 0：%v %v", resp, err)
	}
	db := workspaceTable(t, h)
	resp, err = h.client.DeleteEnvironmentConfig(ctx, authed(h, "t1", &workspacev1.DeleteEnvironmentConfigRequest{TenantId: "t1", EnvironmentId: "env-py"}))
	if err != nil || resp.Msg.GetWorkspaces() != 2 {
		t.Fatalf("u-alice 用 env-py 建的两个工作区：%v %v", resp, err)
	}
	again, err := h.client.DeleteEnvironmentConfig(ctx, authed(h, "t1", &workspacev1.DeleteEnvironmentConfigRequest{TenantId: "t1", EnvironmentId: "env-py"}))
	if err != nil || again.Msg.GetWorkspaces() != 0 {
		t.Fatalf("幂等：%v %v", again, err)
	}
	var rows []relational.Row
	_ = db.Transaction(ctx, relational.TxOptions{ReadOnly: true}, func(ctx context.Context, tx relational.Tx) error {
		rows, err = tx.Query(ctx, `SELECT workspace_id, state, environment, environment_secrets, environment_deleted_at FROM workspaces ORDER BY workspace_id`)
		return err
	})
	deleted := map[string]bool{}
	for _, r := range rows {
		deleted[r["workspace_id"].(string)] = r["environment_deleted_at"].(string) != ""
		if r["state"] != "active" || r["environment"] != `{"setup_script":"x"}` {
			t.Fatal("不删工作区，会话的非机密快照留着")
		}
		if secrets := r["environment_secrets"].(string); (secrets == "") != deleted[r["workspace_id"].(string)] {
			t.Fatalf("机密的部分随删除立即清掉，别的不动：%v", r)
		}
	}
	if !deleted["w1"] || !deleted["w2"] || deleted["w3"] || deleted["w4"] {
		t.Fatalf("只标记调用者自己用这个环境建的：%v", deleted)
	}
	// 别的租户看不到、动不了。
	other, err := h.client.DeleteEnvironmentConfig(ctx, authed(h, "t2", &workspacev1.DeleteEnvironmentConfigRequest{TenantId: "t2", EnvironmentId: "env-py"}))
	if err != nil || other.Msg.GetWorkspaces() != 0 {
		t.Fatalf("别的租户：%v %v", other, err)
	}
	if _, err := h.client.DeleteEnvironmentConfig(ctx, authed(h, "t1", &workspacev1.DeleteEnvironmentConfigRequest{TenantId: "t2", EnvironmentId: "env-py"})); code(t, err) != connect.CodePermissionDenied {
		t.Fatalf("请求的 tenant_id 与身份不一致：%v", err)
	}
	if _, err := h.client.DeleteEnvironmentConfig(ctx, authed(h, "t1", &workspacev1.DeleteEnvironmentConfigRequest{TenantId: "t1", EnvironmentId: "bad id"})); code(t, err) != connect.CodeInvalidArgument {
		t.Fatalf("环境 ID 的格式：%v", err)
	}
	if len(h.w.started) != 0 {
		t.Fatal("删除环境配置不起工作流：清理交给对账")
	}
}

func TestWorkspaceEchoesTheEnvironmentID(t *testing.T) {
	ws := decode(relational.Row{"workspace_id": "w1", "environment_id": "env-py"}, nil)
	if ws.GetEnvironmentId() != "env-py" {
		t.Fatalf("列表里带上选用的环境：%v", ws)
	}
	raw, _ := protojson.Marshal(ws)
	if strings.Contains(string(raw), "setup") {
		t.Fatal("不回显配置的内容")
	}
}

func TestSetWorkspaceEnvironmentBindsOrClearsThroughTheWorker(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	if _, err := h.client.SetWorkspaceEnvironment(ctx, authed(h, "t1", &workspacev1.SetWorkspaceEnvironmentRequest{TenantId: "t1", WorkspaceId: "w1", Environment: sampleEnvironment()})); err != nil {
		t.Fatal(err)
	}
	in := h.w.started[0]
	env, ok := in.GetArgs().AsMap()["environment"].(map[string]any)
	if in.GetOperation() != workflowv1.WorkspaceOperation_WORKSPACE_OPERATION_SET_ENVIRONMENT || !ok || env["environment_id"] != "env-py" || in.GetRequestedBy() != "u-alice" {
		t.Fatalf("工作流的输入不对：%v", in)
	}
	if _, err := h.client.SetWorkspaceEnvironment(ctx, authed(h, "t1", &workspacev1.SetWorkspaceEnvironmentRequest{TenantId: "t1", WorkspaceId: "w1"})); err != nil {
		t.Fatal(err)
	}
	if _, ok := h.w.started[1].GetArgs().AsMap()["environment"]; ok {
		t.Fatal("清除时不带 environment")
	}
	bad := sampleEnvironment()
	bad.Env = "API_TOKEN=x"
	if _, err := h.client.SetWorkspaceEnvironment(ctx, authed(h, "t1", &workspacev1.SetWorkspaceEnvironmentRequest{TenantId: "t1", WorkspaceId: "w1", Environment: bad})); code(t, err) != connect.CodeInvalidArgument {
		t.Fatalf("配置不对：%v", err)
	}
	if len(h.w.started) != 2 {
		t.Fatal("配置不对时不起工作流")
	}
}
