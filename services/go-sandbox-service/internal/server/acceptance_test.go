package server_test

// SBX-01 的验收（docs/plan/sbx-01-sandbox-service-2026-11-03.md 第 6 节三行合在一起，第 9 节：不分期、一次做完）。
//
// 一（只读）：列目录、读文件在挂卷与存档模式下都不建环境；会话里有可写环境时读到没提交的改动，不改它的 busy、不存档；
//     越权（别的租户、别的工作区、..、指出工作区的符号链接）一律拒绝；服务重启不影响；GetDiff、GetLog 在沙箱里跑 git。
// 二（借还与执行）：沙箱、会话、配额、回退、存档的行为与原来 Worker 里的 Broker 一致（原测试的用例逐条搬到这里）；
//     平台注入的环境变量只在服务里；故障注入：服务被杀，借出中的环境按环境表恢复。
// 三（对账与构建）：对账回收到期的会话级环境、删孤儿、记丢失；ENV-02 的 setup 只跑一次、失败销毁；冷的工作区借用时恢复。
// 另：调用方认证（按调用方各一个令牌，task-service 只能调只读方法），租户必填。

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	"connectrpc.com/connect"

	"github.com/agentruntime/agent-runtime/libs/go-core/connecterr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	sandboxv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/sandbox/v1"
	"github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/sandbox/v1/sandboxv1connect"
	workspacev1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/workspace/v1"
)

const (
	s1 = "ses_01HZY2J5K3M4N5P6Q7R8S9T0VW"
	s2 = "ses_01HZY2J5K3M4N5P6Q7R8S9T0VX"
)

// ---------- 认证与租户 ----------

func TestCallersNeedTheirOwnTokenAndTaskServiceOnlyReads(t *testing.T) {
	h := newHarness(t)
	h.mkWorkspace("t1", "w1", map[string]string{"a.txt": "hi"})
	ctx := context.Background()
	anonymous := sandboxv1connect.NewSandboxServiceClient(h.srv.Client(), h.srv.URL, connect.WithInterceptors(caller("worker", "")))
	_, err := anonymous.ReadFile(ctx, connect.NewRequest(&sandboxv1.ReadFileRequest{Tenant: tenant("t1", "w1", ""), WorkspaceId: "w1", Path: "a.txt"}))
	if connectCode(err) != connect.CodeUnauthenticated {
		t.Fatalf("没带令牌：%v", err)
	}
	swapped := sandboxv1connect.NewSandboxServiceClient(h.srv.Client(), h.srv.URL, connect.WithInterceptors(caller("worker", readerToken)))
	_, err = swapped.ReadFile(ctx, connect.NewRequest(&sandboxv1.ReadFileRequest{Tenant: tenant("t1", "w1", ""), WorkspaceId: "w1", Path: "a.txt"}))
	if connectCode(err) != connect.CodeUnauthenticated {
		t.Fatalf("拿别的调用方的令牌冒充：%v", err)
	}
	if _, err := h.reader.ReadFile(ctx, connect.NewRequest(&sandboxv1.ReadFileRequest{Tenant: tenant("t1", "w1", ""), WorkspaceId: "w1", Path: "a.txt"})); err != nil {
		t.Fatalf("task-service 读文件：%v", err)
	}
	_, err = h.reader.Acquire(ctx, connect.NewRequest(&sandboxv1.AcquireRequest{Tenant: tenant("t1", "w1", ""), Spec: &sandboxv1.Spec{}}))
	if connectCode(err) != connect.CodePermissionDenied {
		t.Fatalf("task-service 不能借环境：%v", err)
	}
	_, err = h.worker.ReadFile(ctx, connect.NewRequest(&sandboxv1.ReadFileRequest{WorkspaceId: "w1", Path: "a.txt"}))
	if cat, c := code(t, err); cat != providererr.Denied && cat != providererr.Invalid {
		t.Fatalf("没有租户：%v %s", cat, c)
	}
}

// ---------- 一：只读 ----------

func TestReadsOnAMountedWorkspaceCreateNoEnvironment(t *testing.T) {
	h := newHarness(t)
	h.mkWorkspace("t1", "w1", map[string]string{"src/main.py": "print('hi')\n", "README.md": "# w1\n", "bin.dat": "a\x00b"})
	ctx := context.Background()
	list, err := h.reader.ListFiles(ctx, connect.NewRequest(&sandboxv1.ListFilesRequest{Tenant: tenant("t1", "w1", ""), WorkspaceId: "w1"}))
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, e := range list.Msg.GetResponse().GetEntries() {
		names = append(names, e.GetName())
	}
	if strings.Join(names, ",") != "README.md,bin.dat,src" {
		t.Fatalf("列目录：%v", names)
	}
	read, err := h.reader.ReadFile(ctx, connect.NewRequest(&sandboxv1.ReadFileRequest{Tenant: tenant("t1", "w1", ""), WorkspaceId: "w1", Path: "src/main.py"}))
	if err != nil || read.Msg.GetResponse().GetContent() != "print('hi')\n" {
		t.Fatalf("读文件：%v %v", read, err)
	}
	bin, err := h.reader.ReadFile(ctx, connect.NewRequest(&sandboxv1.ReadFileRequest{Tenant: tenant("t1", "w1", ""), WorkspaceId: "w1", Path: "bin.dat"}))
	if err != nil || !bin.Msg.GetResponse().GetBinary() || bin.Msg.GetResponse().GetContent() != "" {
		t.Fatalf("二进制文件不给内容：%v %v", bin, err)
	}
	grep, err := h.reader.Grep(ctx, connect.NewRequest(&sandboxv1.GrepRequest{Tenant: tenant("t1", "w1", ""), WorkspaceId: "w1", Pattern: "print"}))
	if err != nil || len(grep.Msg.GetMatches()) != 1 || grep.Msg.GetMatches()[0].GetPath() != "src/main.py" {
		t.Fatalf("搜索：%v %v", grep, err)
	}
	if n := h.envCount("t1"); n != 0 {
		t.Fatalf("只读不建环境，环境表里有 %d 行", n)
	}
}

func TestLargeFilesGiveOnlyTheSize(t *testing.T) {
	h := newHarness(t)
	h.mkWorkspace("t1", "w1", map[string]string{"big.txt": strings.Repeat("x", 600<<10)})
	read, err := h.reader.ReadFile(context.Background(), connect.NewRequest(&sandboxv1.ReadFileRequest{Tenant: tenant("t1", "w1", ""), WorkspaceId: "w1", Path: "big.txt"}))
	if err != nil {
		t.Fatal(err)
	}
	r := read.Msg.GetResponse()
	if !r.GetTooLarge() || r.GetContent() != "" || r.GetSize() != 600<<10 {
		t.Fatalf("超过 512 KiB 只给大小：%v", r)
	}
}

func TestReadsRefuseOtherTenantsTraversalAndEscapingSymlinks(t *testing.T) {
	h := newHarness(t)
	root := h.mkWorkspace("t1", "w1", map[string]string{"a.txt": "mine"})
	h.mkWorkspace("t2", "w2", map[string]string{"secret.txt": "theirs"})
	outside := filepath.Join(h.dir, "outside.txt")
	if err := os.WriteFile(outside, []byte("host file"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, filepath.Join(root, "escape.txt")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(h.workspaces, "t2", "w2"), filepath.Join(root, "other")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink("a.txt", filepath.Join(root, "inside.txt")); err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	read := func(tc string, ws, path string) error {
		_, err := h.reader.ReadFile(ctx, connect.NewRequest(&sandboxv1.ReadFileRequest{Tenant: tenant(tc, ws, ""), WorkspaceId: ws, Path: path}))
		return err
	}
	if err := read("t1", "w2", "secret.txt"); err == nil {
		t.Fatal("t1 读到了 t2 的工作区（按 t1/w2 找，不存在）")
	}
	for _, p := range []string{"../../t2/w2/secret.txt", "/etc/passwd", "..\\x", "escape.txt", "other/secret.txt"} {
		if err := read("t1", "w1", p); err == nil {
			t.Fatalf("%s 应当被拒绝", p)
		}
	}
	_, err := h.reader.ListFiles(ctx, connect.NewRequest(&sandboxv1.ListFilesRequest{Tenant: tenant("t1", "w1", ""), WorkspaceId: "w1", Path: "other"}))
	if err == nil {
		t.Fatal("列出指出工作区的符号链接目录应当被拒绝")
	}
	if err := read("t1", "w1", "inside.txt"); err != nil {
		t.Fatalf("指向工作区里面的链接照常读：%v", err)
	}
	if _, err := h.reader.ReadFile(ctx, connect.NewRequest(&sandboxv1.ReadFileRequest{Tenant: tenant("t1", "", ""), WorkspaceId: "../t2", Path: "x"})); err == nil {
		t.Fatal("workspace_id 格式不对要拒绝")
	}
	if n := h.envCount("t1"); n != 0 {
		t.Fatalf("只读不建环境：%d", n)
	}
}

func TestReadsInArchiveModeComeFromTheLatestSnapshot(t *testing.T) {
	h := newHarness(t, withBackends(localArchive))
	tc := tenant("t1", "w1", "")
	lease := h.mustAcquire(tc, onWorkspace("w1"))
	if !lease.GetArchive() {
		t.Fatal("存档模式的后端，Lease 要标 archive")
	}
	h.mustExec(tc, lease, "mkdir -p docs && printf 'from snapshot' > docs/a.md")
	if _, err := h.release(tc, lease, false); err != nil {
		t.Fatalf("还（先存档）：%v", err)
	}
	before := h.envCount("t1")
	ctx := context.Background()
	read, err := h.reader.ReadFile(ctx, connect.NewRequest(&sandboxv1.ReadFileRequest{Tenant: tc, WorkspaceId: "w1", Path: "docs/a.md"}))
	if err != nil || read.Msg.GetResponse().GetContent() != "from snapshot" {
		t.Fatalf("从快照读：%v %v", read, err)
	}
	list, err := h.reader.ListFiles(ctx, connect.NewRequest(&sandboxv1.ListFilesRequest{Tenant: tc, WorkspaceId: "w1", Path: "docs"}))
	if err != nil || len(list.Msg.GetResponse().GetEntries()) != 1 || list.Msg.GetResponse().GetEntries()[0].GetType() != workspacev1.FileType_FILE_TYPE_FILE {
		t.Fatalf("从快照列目录：%v %v", list, err)
	}
	if after := h.envCount("t1"); after != before {
		t.Fatalf("从快照读不建环境：%d → %d", before, after)
	}
}

func TestReadsSeeUncommittedChangesInTheSessionsWritableEnvironment(t *testing.T) {
	h := newHarness(t, withBackends(localArchive))
	tc := tenant("t1", "w1", s1)
	lease := h.mustAcquire(tc, onWorkspace("w1"), reuse())
	h.mustExec(tc, lease, "printf 'draft' > notes.txt")
	if _, err := h.release(tc, lease, true); err != nil {
		t.Fatal(err)
	}
	busy := h.rows("t1", "SELECT busy_until, last_used_at FROM sandbox_envs WHERE env_ref = $1", lease.GetLeaseId())[0]
	read, err := h.reader.ReadFile(context.Background(), connect.NewRequest(&sandboxv1.ReadFileRequest{Tenant: tc, WorkspaceId: "w1", Path: "notes.txt"}))
	if err != nil || read.Msg.GetResponse().GetContent() != "draft" {
		t.Fatalf("会话里活着的可写环境里读到没存档的改动：%v %v", read, err)
	}
	after := h.rows("t1", "SELECT busy_until, last_used_at FROM sandbox_envs WHERE env_ref = $1", lease.GetLeaseId())[0]
	if fmt.Sprint(after) != fmt.Sprint(busy) {
		t.Fatalf("只读不改环境的 busy 与使用时间：%v → %v", busy, after)
	}
	if n := len(h.rows("t1", "SELECT snapshot_id FROM workspace_snapshots WHERE workspace_id = 'w1'")); n != 0 {
		t.Fatalf("只读不触发存档：%d 份快照", n)
	}
}

func TestReadsKeepWorkingAcrossARestart(t *testing.T) {
	h := newHarness(t)
	h.mkWorkspace("t1", "w1", map[string]string{"a.txt": "one"})
	h.restart()
	read, err := h.reader.ReadFile(context.Background(), connect.NewRequest(&sandboxv1.ReadFileRequest{Tenant: tenant("t1", "w1", ""), WorkspaceId: "w1", Path: "a.txt"}))
	if err != nil || read.Msg.GetResponse().GetContent() != "one" {
		t.Fatalf("重启之后照常读：%v %v", read, err)
	}
}

func TestDiffAndLogRunGitInASandboxNotInTheService(t *testing.T) {
	h := newHarness(t)
	root := h.mkWorkspace("t1", "w1", map[string]string{"a.txt": "one\n"})
	git := func(args ...string) {
		cmd := gitCmd(root, args...)
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("git %v：%v %s", args, err, out)
		}
	}
	git("init", "-q", "-b", "main")
	git("add", "-A")
	git("-c", "user.name=T", "-c", "user.email=t@example.test", "commit", "-qm", "first commit")
	if err := os.WriteFile(filepath.Join(root, "a.txt"), []byte("one\ntwo\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "new.txt"), []byte("new\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	tc := tenant("t1", "w1", "")
	diff, err := h.reader.GetDiff(ctx, connect.NewRequest(&sandboxv1.GetDiffRequest{Tenant: tc, WorkspaceId: "w1"}))
	if err != nil {
		t.Fatal(err)
	}
	d := diff.Msg.GetResponse()
	files := append([]string(nil), d.GetFiles()...)
	sort.Strings(files)
	if !contains(d.GetDiff(), "+two") || strings.Join(files, ",") != "a.txt,new.txt" || d.GetBranch() != "main" || len(d.GetHead()) != 40 {
		t.Fatalf("diff：%+v", d)
	}
	log, err := h.reader.GetLog(ctx, connect.NewRequest(&sandboxv1.GetLogRequest{Tenant: tc, WorkspaceId: "w1"}))
	if err != nil || len(log.Msg.GetResponse().GetCommits()) != 1 || log.Msg.GetResponse().GetCommits()[0].GetSubject() != "first commit" {
		t.Fatalf("log：%v %v", log, err)
	}
	// git 在沙箱里跑（短命的只读环境，用完即毁）：环境表里有记录，且都已结束。
	for _, row := range h.rows("t1", "SELECT state, writable FROM sandbox_envs") {
		if fmt.Sprint(row["state"]) != "destroyed" || fmt.Sprint(row["writable"]) != "0" {
			t.Fatalf("diff、log 用只读的短命环境：%v", row)
		}
	}
}

// Workspace API 的 GetDiff 等直接调服务（WS-READ-01）：错误与原来经 Worker 时一样分类。别的租户、没有的工作区是 not_found
// （存档模式下不借环境就报），base 不在仓库里也是 not_found（git_not_found），不是 git_failed。
func TestDiffErrorsAreClassifiedLikeTheWorkerDid(t *testing.T) {
	for _, mode := range []string{localMount, localArchive} {
		h := newHarness(t, withBackends(mode))
		ctx := context.Background()
		_, err := h.reader.GetDiff(ctx, connect.NewRequest(&sandboxv1.GetDiffRequest{Tenant: tenant("t2", "w1", ""), WorkspaceId: "w1"}))
		if cat, c := code(t, err); cat != providererr.NotFound || c != "workspace_not_found" {
			t.Fatalf("%s 别的租户没有这个工作区：%v %s", mode, cat, c)
		}
		if mode == localArchive {
			if n := len(h.rows("t2", "SELECT env_ref FROM sandbox_envs")); n != 0 {
				t.Fatalf("没有的工作区不借环境：%d", n)
			}
		}
	}
	h := newHarness(t)
	root := h.mkWorkspace("t1", "w1", map[string]string{"a.txt": "one\n"})
	for _, args := range [][]string{{"init", "-q", "-b", "main"}, {"add", "-A"}, {"-c", "user.name=T", "-c", "user.email=t@example.test", "commit", "-qm", "first"}} {
		if out, err := gitCmd(root, args...).CombinedOutput(); err != nil {
			t.Fatalf("git %v：%v %s", args, err, out)
		}
	}
	_, err := h.reader.GetDiff(context.Background(), connect.NewRequest(&sandboxv1.GetDiffRequest{Tenant: tenant("t1", "w1", ""), WorkspaceId: "w1",
		Base: "eb83d60c238dcf0fbfb35347541a272bdc339296"}))
	if cat, c := code(t, err); cat != providererr.NotFound || c != "git_not_found" {
		t.Fatalf("base 不在仓库里：%v %s", cat, c)
	}
}

// ---------- 二：借还与执行 ----------

func TestOneShotLeaseIsDestroyedOnRelease(t *testing.T) {
	h := newHarness(t)
	h.mkWorkspace("t1", "w1", nil)
	tc := tenant("t1", "w1", "")
	lease := h.mustAcquire(tc, onWorkspace("w1"))
	if out := h.mustExec(tc, lease, "printf hello > hello.txt && cat hello.txt"); out != "hello" {
		t.Fatalf("exec：%q", out)
	}
	if b, err := os.ReadFile(filepath.Join(h.workspaces, "t1", "w1", "hello.txt")); err != nil || string(b) != "hello" {
		t.Fatalf("挂卷：命令写的文件在工作区里：%q %v", b, err)
	}
	done, err := h.release(tc, lease, false)
	if err != nil {
		t.Fatal(err)
	}
	if h.envState("t1", lease.GetLeaseId()) != "destroyed" || done.GetRecord().GetState().String() != "ENVIRONMENT_STATE_DESTROYED" {
		t.Fatalf("一次性的环境还了就销毁：%v", done.GetRecord())
	}
	if _, err := h.exec(tc, lease, "true"); err == nil {
		t.Fatal("销毁之后不能再用")
	}
}

func TestPlatformVariablesAreInjectedButNeverReturned(t *testing.T) {
	h := newHarness(t, withGateways("http://pkg-mirror.test:8093", "http://egress.test:8095"))
	h.mkWorkspace("t1", "w1", nil)
	tc := tenant("t1", "w1", "")
	lease := h.mustAcquire(tc, onWorkspace("w1"))
	out := h.mustExec(tc, lease, `printf '%s|%s|%s' "$PIP_INDEX_URL" "$HTTPS_PROXY" "$TEST_API_BASE"`)
	parts := strings.Split(out, "|")
	if !strings.HasPrefix(parts[0], "http://pkg-mirror.test:8093/t/ar1.") || !strings.Contains(parts[1], "@egress.test:8095") || parts[2] != "https://api.example.test" {
		t.Fatalf("包镜像、出网代理与测试用密钥：%q", out)
	}
	if lease.GetNetwork() != "gateway" || strings.Join(lease.GetTestEnvNames(), ",") != "TEST_API_BASE" {
		t.Fatalf("Lease 只给名字与联网方式：%v", lease)
	}
	if text := fmt.Sprint(lease); strings.Contains(text, "ar1.") || strings.Contains(text, "api.example.test") {
		t.Fatalf("Lease 里不能有凭据：%s", text)
	}
	// 调用方的变量盖不掉平台的。
	resp, err := h.worker.Exec(context.Background(), connect.NewRequest(&sandboxv1.ExecRequest{
		Tenant: tc, LeaseId: lease.GetLeaseId(), Argv: []string{"sh", "-c", `printf %s "$HTTPS_PROXY"`}, TimeoutMs: 10_000,
		Env: map[string]string{"HTTPS_PROXY": "http://evil.test"},
	}))
	if err == nil && strings.Contains(string(resp.Msg.GetStdout()), "evil") {
		t.Fatal("调用方的变量盖掉了平台注入的出网代理")
	}
}

func TestSessionLeasesAreReusedAcrossCallsAndReplicas(t *testing.T) {
	h := newHarness(t)
	h.mkWorkspace("t1", "w1", nil)
	a := tenant("t1", "w1", s1)
	first := h.mustAcquire(a, onWorkspace("w1"), reuse())
	h.mustExec(a, first, "nohup sleep 300 >/dev/null 2>&1 & echo $! > /tmp/sbx-test-$PPID.pid; echo started")
	if _, err := h.release(a, first, true); err != nil {
		t.Fatal(err)
	}
	second := h.mustAcquire(a, onWorkspace("w1"), reuse())
	if second.GetLeaseId() != first.GetLeaseId() || !second.GetReused() {
		t.Fatalf("同一会话复用同一个环境：%s %s reused=%v", first.GetLeaseId(), second.GetLeaseId(), second.GetReused())
	}
	if _, err := h.release(a, second, true); err != nil {
		t.Fatal(err)
	}
	b := tenant("t1", "w1", s2)
	other, err := h.acquire(b, onWorkspace("w1"), reuse())
	if err != nil {
		t.Fatalf("别的会话：上一个闲着就回收它再建：%v", err)
	}
	if other.GetLeaseId() == first.GetLeaseId() {
		t.Fatal("不同会话各用各的环境")
	}
	if h.envState("t1", first.GetLeaseId()) != "destroyed" {
		t.Fatal("别的会话要写同一个工作区：闲着的可写环境被回收")
	}
}

func TestBusyWritableEnvironmentOfAnotherSessionIsNotPreempted(t *testing.T) {
	h := newHarness(t)
	h.mkWorkspace("t1", "w1", nil)
	a := tenant("t1", "w1", s1)
	first := h.mustAcquire(a, onWorkspace("w1"), reuse(), busyFor(10*time.Minute))
	_, err := h.acquire(tenant("t1", "w1", s2), onWorkspace("w1"), reuse())
	if cat, c := code(t, err); cat != providererr.Denied || c != "workspace_busy" {
		t.Fatalf("正在跑命令的不回收：%v %s", cat, c)
	}
	if h.envState("t1", first.GetLeaseId()) != "ready" {
		t.Fatal("忙的环境照常活着")
	}
}

func TestOtherTenantsCannotUseALease(t *testing.T) {
	h := newHarness(t)
	h.mkWorkspace("t1", "w1", nil)
	lease := h.mustAcquire(tenant("t1", "w1", s1), onWorkspace("w1"), reuse())
	_, err := h.exec(tenant("t2", "w1", s1), lease, "cat /etc/hostname")
	if cat, _ := code(t, err); cat != providererr.NotFound {
		t.Fatalf("别的租户拿 lease_id 用：%v", err)
	}
	_, err = h.release(tenant("t2", "w1", s1), lease, false)
	if cat, _ := code(t, err); cat != providererr.NotFound {
		t.Fatalf("别的租户还不了：%v", err)
	}
	if h.envState("t1", lease.GetLeaseId()) != "ready" {
		t.Fatal("环境照常活着")
	}
}

func TestPathsInLeaseFileOperationsStayInTheWorkdir(t *testing.T) {
	h := newHarness(t)
	h.mkWorkspace("t1", "w1", nil)
	tc := tenant("t1", "w1", "")
	lease := h.mustAcquire(tc, onWorkspace("w1"))
	ctx := context.Background()
	for _, p := range []string{"../x", "/etc/passwd", "a/../../b", "a\\b"} {
		_, err := h.worker.WriteFile(ctx, connect.NewRequest(&sandboxv1.WriteFileRequest{Tenant: tc, LeaseId: lease.GetLeaseId(), Path: p, Data: []byte("x")}))
		if cat, _ := code(t, err); cat != providererr.Invalid {
			t.Fatalf("写 %s：%v", p, err)
		}
	}
	if _, err := h.worker.WriteFile(ctx, connect.NewRequest(&sandboxv1.WriteFileRequest{Tenant: tc, LeaseId: lease.GetLeaseId(), Path: "d/x.txt", Data: []byte("ok")})); err != nil {
		t.Fatal(err)
	}
	got, err := h.worker.ReadLeaseFile(ctx, connect.NewRequest(&sandboxv1.ReadLeaseFileRequest{Tenant: tc, LeaseId: lease.GetLeaseId(), Path: "d/x.txt"}))
	if err != nil || string(got.Msg.GetData()) != "ok" {
		t.Fatalf("读回：%v %v", got, err)
	}
}

func quotaCase(t *testing.T, opts ...option) {
	h := newHarness(t, append(opts, withPolicy("ta", map[string]any{"sandbox": map[string]any{"backend": "local", "quota": map[string]any{"max_concurrent": 5}}}))...)
	var wg sync.WaitGroup
	var mu sync.Mutex
	ok, denied := 0, 0
	codes := map[string]int{}
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, err := h.acquire(tenant("ta", "", ""))
			mu.Lock()
			defer mu.Unlock()
			if err == nil {
				ok++
				return
			}
			denied++
			pe := decode(err)
			codes[pe]++
		}()
	}
	wg.Wait()
	if ok != 5 || denied != 15 || codes["sandbox_quota_exceeded"] != 15 {
		t.Fatalf("20 个并发、配额 5：成功 %d、拒绝 %d %v", ok, denied, codes)
	}
}

func TestTwentyConcurrentAcquiresWithQuotaFiveGiveExactlyFiveSQLite(t *testing.T) { quotaCase(t) }

func TestTwentyConcurrentAcquiresWithQuotaFiveGiveExactlyFivePostgres(t *testing.T) {
	quotaCase(t, withPostgres())
}

func TestBudgetsAreDeniedAsBudgetExceeded(t *testing.T) {
	h := newHarness(t, withPolicy("ta", map[string]any{"sandbox": map[string]any{"backend": "local", "quota": map[string]any{"daily_cost_usd": 1.0}}}))
	if _, err := h.acquire(tenant("ta", "", "")); err != nil {
		t.Fatal(err) // 建一次让表就位
	}
	h.execSQL("ta", "INSERT INTO sandbox_usage (day, cost_usd) VALUES ($1, $2) ON CONFLICT (day) DO UPDATE SET cost_usd = $2",
		h.clock.Now().UTC().Format("2006-01-02"), 1.5)
	_, err := h.acquire(tenant("ta", "", ""))
	if cat, c := code(t, err); cat != providererr.Denied || c != "sandbox_budget_exceeded" {
		t.Fatalf("超了当天预算：%v %s", cat, c)
	}
}

const fourBackends = `{"default": "gvisor", "backends": {
  "gvisor": {"kind": "local", "isolation": "container"},
  "kata": {"kind": "local", "isolation": "microvm"},
  "cloudflare": {"kind": "local", "isolation": "microvm"},
  "plain": {"kind": "local", "isolation": "none"}}}`

func TestTransientOnThePreferredBackendFallsBackAndIsRecorded(t *testing.T) {
	h := newHarness(t, withBackends(fourBackends), withPolicy("ta", map[string]any{"sandbox": map[string]any{"backend": "kata", "fallback": []any{"gvisor", "cloudflare"}}}))
	h.faults["kata"].FailNext("create", providererr.Transient)
	lease := h.mustAcquire(tenant("ta", "", ""))
	if lease.GetBackend().GetName() != "cloudflare" || lease.GetFallbackFrom() != "kata" || !strings.HasPrefix(lease.GetFallbackReason(), "kata：") {
		t.Fatalf("gvisor 比 kata 弱，跳过；落到同为 microvm 的 cloudflare：%v", lease)
	}
	failed := h.rows("ta", "SELECT backend, reason FROM sandbox_envs WHERE state = 'failed'")
	if len(failed) != 1 || fmt.Sprint(failed[0]["backend"]) != "kata" || fmt.Sprint(failed[0]["reason"]) == "" {
		t.Fatalf("首选失败的那一行：%v", failed)
	}
}

func TestDeniedAndInvalidAreNotRetriedElsewhere(t *testing.T) {
	h := newHarness(t, withBackends(fourBackends), withPolicy("ta", map[string]any{"sandbox": map[string]any{"backend": "kata", "fallback": []any{"cloudflare"}}}))
	for _, cat := range []providererr.Category{providererr.Denied, providererr.Invalid} {
		h.faults["kata"].FailNext("create", cat)
		_, err := h.acquire(tenant("ta", "", ""))
		if got, _ := code(t, err); got != cat {
			t.Fatalf("%v 原样报出：%v", cat, got)
		}
	}
	if n := len(h.rows("ta", "SELECT env_ref FROM sandbox_envs WHERE backend = 'cloudflare'")); n != 0 {
		t.Fatal("没有回退到 cloudflare")
	}
}

func TestOnlyWeakerBackendsLeftIsRefused(t *testing.T) {
	h := newHarness(t, withBackends(fourBackends), withPolicy("ta", map[string]any{"sandbox": map[string]any{"backend": "kata", "fallback": []any{"gvisor", "plain"}}}))
	h.faults["kata"].FailNext("create", providererr.Transient)
	_, err := h.acquire(tenant("ta", "", ""))
	cat, _ := code(t, err)
	if cat != providererr.Transient || !strings.Contains(err.Error(), "gvisor") || !strings.Contains(err.Error(), "plain") {
		t.Fatalf("绝不降级，说明更弱的后端不用：%v", err)
	}
}

func TestPolicyErrorsAreRefused(t *testing.T) {
	for policy, want := range map[string]string{
		"firecracker-x": "unknown_sandbox_backend",
		"vm":            "invalid_min_isolation",
	} {
		p := map[string]any{"sandbox": map[string]any{"backend": "gvisor", "fallback": []any{policy}}}
		if want == "invalid_min_isolation" {
			p = map[string]any{"sandbox": map[string]any{"backend": "gvisor", "min_isolation": policy}}
		}
		h := newHarness(t, withBackends(fourBackends), withPolicy("ta", p))
		_, err := h.acquire(tenant("ta", "", ""))
		if cat, c := code(t, err); cat != providererr.Invalid || c != want {
			t.Fatalf("%s：%v %s", want, cat, c)
		}
	}
}

func TestDisabledBackendTakesNoNewEnvironments(t *testing.T) {
	doc := `{"default": "a", "backends": {"a": {"kind": "local", "isolation": "none"}, "b": {"kind": "local", "isolation": "none", "enabled": false}}}`
	h := newHarness(t, withBackends(doc), withPolicy("ta", map[string]any{"sandbox": map[string]any{"backend": "b"}}))
	_, err := h.acquire(tenant("ta", "", ""))
	if cat, c := code(t, err); cat != providererr.Denied || c != "sandbox_backend_disabled" {
		t.Fatalf("停用的后端不接新环境：%v %s", cat, c)
	}
}

func TestArchiveModeSnapshotsOnReleaseAndRestoresOnTheNextLease(t *testing.T) {
	h := newHarness(t, withBackends(localArchive))
	tc := tenant("t1", "w1", "")
	first := h.mustAcquire(tc, onWorkspace("w1"))
	h.mustExec(tc, first, "mkdir -p src && printf 'v1' > src/app.txt && ln -s src/app.txt link")
	if _, err := h.release(tc, first, false); err != nil {
		t.Fatal(err)
	}
	snaps := h.rows("t1", "SELECT snapshot_id FROM workspace_snapshots WHERE workspace_id = 'w1' AND deleted_at = ''")
	if len(snaps) != 1 {
		t.Fatalf("可写的存档模式环境还之前存档：%v", snaps)
	}
	second := h.mustAcquire(tc, onWorkspace("w1"), busyFor(10*time.Minute))
	if second.GetRestoredSnapshot() != fmt.Sprint(snaps[0]["snapshot_id"]) {
		t.Fatalf("新环境从最近的快照恢复：%v", second)
	}
	if out := h.mustExec(tc, second, "cat src/app.txt && readlink link"); out != "v1src/app.txt\n" {
		t.Fatalf("恢复的内容：%q", out)
	}
	_, err := h.acquire(tenant("t1", "w1", s2), onWorkspace("w1"), busyFor(time.Minute))
	if cat, c := code(t, err); cat != providererr.Denied || c != "workspace_busy" {
		t.Fatalf("存档模式下一个工作区同一时间只有一个可写环境：%v %s", cat, c)
	}
}

func TestSnapshotRPCAndManifestAndArchiveStreams(t *testing.T) {
	h := newHarness(t)
	h.mkWorkspace("t1", "w1", map[string]string{"a.txt": "alpha", "d/b.txt": "beta"})
	tc := tenant("t1", "w1", "")
	lease := h.mustAcquire(tc, onWorkspace("w1"))
	ctx := context.Background()
	snap, err := h.worker.Snapshot(ctx, connect.NewRequest(&sandboxv1.SnapshotRequest{Tenant: tc, LeaseId: lease.GetLeaseId()}))
	if err != nil || snap.Msg.GetSnapshotId() == "" || snap.Msg.GetFiles() != 2 {
		t.Fatalf("挂卷的后端一样能存快照：%v %v", snap, err)
	}
	man, err := h.worker.Manifest(ctx, connect.NewRequest(&sandboxv1.ManifestRequest{Tenant: tc, LeaseId: lease.GetLeaseId()}))
	if err != nil || len(man.Msg.GetEntries()) != 3 {
		t.Fatalf("清单：%v %v", man, err)
	}
	stream, err := h.worker.ExportArchive(ctx, connect.NewRequest(&sandboxv1.ExportArchiveRequest{Tenant: tc, LeaseId: lease.GetLeaseId(), MaxBytes: 1 << 20}))
	if err != nil {
		t.Fatal(err)
	}
	var tarBytes []byte
	for stream.Receive() {
		tarBytes = append(tarBytes, stream.Msg().GetData()...)
	}
	if err := stream.Err(); err != nil || len(tarBytes) < 1024 {
		t.Fatalf("导出：%d 字节 %v", len(tarBytes), err)
	}
	other := h.mustAcquire(tenant("t1", "", ""))
	up := h.worker.ImportArchive(ctx)
	if err := up.Send(&sandboxv1.ImportArchiveRequest{Part: &sandboxv1.ImportArchiveRequest_Header{Header: &sandboxv1.ImportArchiveHeader{
		Tenant: tenant("t1", "", ""), LeaseId: other.GetLeaseId(), Size: int64(len(tarBytes)), MaxBytes: 1 << 20}}}); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < len(tarBytes); i += 1000 {
		end := min(i+1000, len(tarBytes))
		if err := up.Send(&sandboxv1.ImportArchiveRequest{Part: &sandboxv1.ImportArchiveRequest_Chunk{Chunk: tarBytes[i:end]}}); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := up.CloseAndReceive(); err != nil {
		t.Fatalf("导入：%v", err)
	}
	if out := h.mustExec(tenant("t1", "", ""), other, "cat d/b.txt"); out != "beta" {
		t.Fatalf("导入的内容：%q", out)
	}
}

// 故障注入：服务被杀时有一个借出中的会话级环境；新起的服务按环境表接上它，调用方照常用同一个 lease_id。
func TestServiceKilledLeasedEnvironmentIsRecoveredFromTheEnvTable(t *testing.T) {
	h := newHarness(t)
	h.mkWorkspace("t1", "w1", nil)
	tc := tenant("t1", "w1", s1)
	lease := h.mustAcquire(tc, onWorkspace("w1"), reuse(), busyFor(10*time.Minute))
	h.mustExec(tc, lease, "printf before > state.txt")
	h.restart()
	if out := h.mustExec(tc, lease, "cat state.txt"); out != "before" {
		t.Fatalf("重启后照常在同一个环境里执行：%q", out)
	}
	if _, err := h.release(tc, lease, true); err != nil {
		t.Fatalf("重启后照常还：%v", err)
	}
	again := h.mustAcquire(tc, onWorkspace("w1"), reuse())
	if again.GetLeaseId() != lease.GetLeaseId() || !again.GetReused() {
		t.Fatalf("重启后同一会话照常复用：%v", again)
	}
	if h.envCount("t1") != 1 {
		t.Fatal("没有多建环境")
	}
}

// ---------- 三：对账、环境配置、冷热分层 ----------

func TestReconcileReapsIdleSessionEnvironmentsAndOrphans(t *testing.T) {
	h := newHarness(t)
	h.mkWorkspace("t1", "w1", nil)
	tc := tenant("t1", "w1", s1)
	lease := h.mustAcquire(tc, onWorkspace("w1"), reuse())
	if _, err := h.release(tc, lease, true); err != nil {
		t.Fatal(err)
	}
	orphan, err := h.box("local", "t1").Create(context.Background(), sandbox.Spec{Runtime: "python"})
	if err != nil {
		t.Fatal(err)
	}
	h.clock.Advance(16 * time.Minute)
	report, err := h.svc.Reconcile(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if h.envState("t1", lease.GetLeaseId()) != "destroyed" || report.Reaped != 1 {
		t.Fatalf("空闲到期的会话级环境被回收：%+v", report)
	}
	if report.OrphansDestroyed != 1 {
		t.Fatalf("后端里有、环境表里没有的环境（%s）超过宽限被删：%+v", orphan.ID, report)
	}
	again := h.mustAcquire(tc, onWorkspace("w1"), reuse())
	if again.GetReused() {
		t.Fatal("回收之后换了新环境")
	}
}

func TestReconcileMarksVanishedEnvironmentsLost(t *testing.T) {
	h := newHarness(t)
	h.mkWorkspace("t1", "w1", nil)
	tc := tenant("t1", "w1", s1)
	lease := h.mustAcquire(tc, onWorkspace("w1"), reuse())
	if err := h.box("local", "t1").Destroy(context.Background(), lease.GetBackendEnvId()); err != nil {
		t.Fatal(err)
	}
	if _, err := h.svc.Reconcile(context.Background()); err != nil {
		t.Fatal(err)
	}
	if h.envState("t1", lease.GetLeaseId()) != "lost" {
		t.Fatal("后端里没了：记为丢失")
	}
}

func setEnvironment(h *harness, owner, ws, public string) {
	h.execSQL(owner, "UPDATE workspaces SET environment_id = 'env-a', environment = $1, environment_owner = 'u1' WHERE workspace_id = $2", public, ws)
}

func TestSetupRunsOncePerEnvironmentAndFailureDestroysIt(t *testing.T) {
	h := newHarness(t, withBackends(localArchive))
	tc := tenant("t1", "w1", s1)
	first := h.mustAcquire(tc, onWorkspace("w1"), reuse())
	if _, err := h.release(tc, first, false); err != nil { // 让工作区有一行
		t.Fatal(err)
	}
	setEnvironment(h, "t1", "w1", `{"environment_id":"env-a","network":{"hosts":[],"mode":""},"setup_script":"echo x >> /tmp/setup-count-$PPID; echo done > setup.txt","start_command":""}`)
	lease := h.mustAcquire(tc, onWorkspace("w1"), reuse(), prepare())
	if lease.GetPrepared().GetSetup() != "succeeded" {
		t.Fatalf("新环境先跑 setup：%v", lease.GetPrepared())
	}
	if out := h.mustExec(tc, lease, "cat setup.txt"); out != "done\n" {
		t.Fatalf("setup 在工作区里跑：%q", out)
	}
	if _, err := h.release(tc, lease, true); err != nil {
		t.Fatal(err)
	}
	again := h.mustAcquire(tc, onWorkspace("w1"), reuse(), prepare())
	if again.GetPrepared() != nil {
		t.Fatalf("复用已准备好的环境不再跑 setup：%v", again.GetPrepared())
	}
	if _, err := h.release(tc, again, false); err != nil {
		t.Fatal(err)
	}
	setEnvironment(h, "t1", "w1", `{"environment_id":"env-a","network":{"hosts":[],"mode":""},"setup_script":"exit 3","start_command":""}`)
	_, err := h.acquire(tenant("t1", "w1", s2), onWorkspace("w1"), reuse(), prepare())
	if cat, c := code(t, err); cat != providererr.Invalid || c != "environment_setup_failed" {
		t.Fatalf("setup 失败：%v %s", cat, c)
	}
	rows := h.rows("t1", "SELECT state, setup_state, setup_error FROM sandbox_envs WHERE session_id = $1", s2)
	if len(rows) != 1 || fmt.Sprint(rows[0]["state"]) != "destroyed" || fmt.Sprint(rows[0]["setup_state"]) != "failed" {
		t.Fatalf("setup 失败的环境记下并销毁：%v", rows)
	}
}

func TestColdWorkspaceIsRestoredWhenLeased(t *testing.T) {
	h := newHarness(t)
	root := h.mkWorkspace("t1", "w1", map[string]string{"keep.txt": "kept"})
	tc := tenant("t1", "w1", "")
	lease := h.mustAcquire(tc, onWorkspace("w1"))
	ctx := context.Background()
	if _, err := h.worker.Snapshot(ctx, connect.NewRequest(&sandboxv1.SnapshotRequest{Tenant: tc, LeaseId: lease.GetLeaseId()})); err != nil {
		t.Fatal(err)
	}
	if _, err := h.release(tc, lease, false); err != nil {
		t.Fatal(err)
	}
	if err := os.RemoveAll(root); err != nil {
		t.Fatal(err)
	}
	h.execSQL("t1", "UPDATE workspaces SET state = 'cold' WHERE workspace_id = 'w1'")
	warm := h.mustAcquire(tc, onWorkspace("w1"))
	if warm.GetRestoredSnapshot() == "" {
		t.Fatal("冷的工作区借用时从快照恢复")
	}
	if out := h.mustExec(tc, warm, "cat keep.txt"); out != "kept" {
		t.Fatalf("恢复的内容：%q", out)
	}
	if st := h.rows("t1", "SELECT state FROM workspaces WHERE workspace_id = 'w1'"); fmt.Sprint(st[0]["state"]) != "active" {
		t.Fatalf("恢复后是 active：%v", st)
	}
}

func TestDestroyWorkspaceAndSession(t *testing.T) {
	h := newHarness(t)
	h.mkWorkspace("t1", "w1", nil)
	tc := tenant("t1", "w1", s1)
	lease := h.mustAcquire(tc, onWorkspace("w1"), reuse())
	ctx := context.Background()
	got, err := h.worker.DestroySession(ctx, connect.NewRequest(&sandboxv1.DestroySessionRequest{Tenant: tc, WorkspaceId: "w1", SessionId: s1}))
	if err != nil || got.Msg.GetDestroyed() != 1 || h.envState("t1", lease.GetLeaseId()) != "destroyed" {
		t.Fatalf("会话关闭时销毁它的环境：%v %v", got, err)
	}
	second := h.mustAcquire(tc, onWorkspace("w1"), reuse(), busyFor(time.Hour))
	gone, err := h.worker.DestroyWorkspace(ctx, connect.NewRequest(&sandboxv1.DestroyWorkspaceRequest{Tenant: tc, WorkspaceId: "w1"}))
	if err != nil || gone.Msg.GetDestroyed() != 1 || h.envState("t1", second.GetLeaseId()) != "destroyed" {
		t.Fatalf("删工作区时连正在跑的也销毁：%v %v", gone, err)
	}
}

func TestDescribeTenant(t *testing.T) {
	h := newHarness(t, withBackends(localArchive))
	got, err := h.reader.DescribeTenant(context.Background(), connect.NewRequest(&sandboxv1.DescribeTenantRequest{Tenant: tenant("t1", "", "")}))
	if err != nil || !got.Msg.GetArchiveMode() || got.Msg.GetBackends()[0] != "local" || got.Msg.GetWorkspaceMaxBytes() != 2<<30 {
		t.Fatalf("租户的沙箱策略摘要：%v %v", got, err)
	}
}

func decode(err error) string {
	if pe := connecterr.Decode(err); pe != nil {
		return pe.Code
	}
	return err.Error()
}

// ---------- 契约草案追加的三项（flush_writable、skip_snapshot、DestroyWorkspace 删卷上的目录）----------

func TestFlushWritableSnapshotsIdleHoldersBeforeAReadOnlyLease(t *testing.T) {
	h := newHarness(t, withBackends(localArchive))
	writer := tenant("t1", "w1", s1)
	held := h.mustAcquire(writer, onWorkspace("w1"), reuse())
	h.mustExec(writer, held, "printf fresh > f.txt")
	if _, err := h.release(writer, held, true); err != nil {
		t.Fatal(err)
	}
	reader, err := h.acquire(tenant("t1", "w1", ""), onWorkspace("w1"), readOnly(), func(r *sandboxv1.AcquireRequest) { r.FlushWritable = true })
	if err != nil {
		t.Fatal(err)
	}
	if out := h.mustExec(tenant("t1", "w1", ""), reader, "cat f.txt"); out != "fresh" {
		t.Fatalf("只读环境看到别人闲着的可写环境里最新的内容：%q", out)
	}
	if h.envState("t1", held.GetLeaseId()) != "ready" {
		t.Fatal("存一份快照，不销毁那个环境")
	}
	busy := h.mustAcquire(tenant("t1", "w2", s2), onWorkspace("w2"), reuse(), busyFor(time.Hour))
	_ = busy
	_, err = h.acquire(tenant("t1", "w2", ""), onWorkspace("w2"), readOnly(), func(r *sandboxv1.AcquireRequest) { r.FlushWritable = true })
	if cat, c := code(t, err); cat != providererr.Denied || c != "workspace_busy" {
		t.Fatalf("有命令在跑时 workspace_busy：%v %s", cat, c)
	}
}

func TestSkipSnapshotDestroysWithoutArchiving(t *testing.T) {
	h := newHarness(t, withBackends(localArchive))
	tc := tenant("t1", "w1", "")
	lease := h.mustAcquire(tc, onWorkspace("w1"))
	h.mustExec(tc, lease, "printf half > half.txt")
	_, err := h.worker.Release(context.Background(), connect.NewRequest(&sandboxv1.ReleaseRequest{Tenant: tc, LeaseId: lease.GetLeaseId(), SkipSnapshot: true}))
	if err != nil {
		t.Fatal(err)
	}
	if n := len(h.rows("t1", "SELECT snapshot_id FROM workspace_snapshots WHERE workspace_id = 'w1'")); n != 0 {
		t.Fatalf("skip_snapshot：不存档，%d 份快照", n)
	}
	if h.envState("t1", lease.GetLeaseId()) != "destroyed" {
		t.Fatal("环境照常销毁")
	}
}

func TestDestroyWorkspaceRemovesTheMountedDirectory(t *testing.T) {
	h := newHarness(t)
	root := h.mkWorkspace("t1", "w1", map[string]string{"a.txt": "x"})
	tc := tenant("t1", "w1", "")
	if _, err := h.worker.DestroyWorkspace(context.Background(), connect.NewRequest(&sandboxv1.DestroyWorkspaceRequest{Tenant: tc, WorkspaceId: "w1"})); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(root); !os.IsNotExist(err) {
		t.Fatalf("挂卷时删掉卷上的工作区目录：%v", err)
	}
}

// notStopping 让 Exec 在调用方取消时像沙箱没确认停下那样报错（旧的 agent 没有取消接口，或经桥时断开传不到）。
type notStopping struct{ sandbox.Provider }

func (p notStopping) Bind(t *commonv1.TenantContext) (sandbox.Sandbox, error) {
	inner, err := p.Provider.Bind(t)
	return notStoppingBox{inner}, err
}

type notStoppingBox struct{ sandbox.Sandbox }

func (b notStoppingBox) Exec(ctx context.Context, envID string, argv []string, opts sandbox.ExecOptions) (sandbox.ExecResult, error) {
	if len(argv) == 3 && argv[2] == "hang" {
		<-ctx.Done()
		return sandbox.ExecResult{}, fmt.Errorf("%w（%w）", ctx.Err(), sandbox.ErrExecNotStopped)
	}
	return b.Sandbox.Exec(ctx, envID, argv, opts)
}

// 打断 Task 时 Worker 取消 Exec，再在同一个环境里还原检查点（docs/plan/running-task-interaction.md 第 2 节）。沙箱没确认命令
// 停了时，那条命令可能还在改文件：之后的命令（还原）报 conflict（exec_not_stopped），Worker 照实说没还原；还掉之后恢复正常。
func TestExecAfterAnUnconfirmedCancelIsRefusedUntilRelease(t *testing.T) {
	h := newHarness(t, withWrappedProvider("local", func(p sandbox.Provider) sandbox.Provider { return notStopping{p} }))
	tc := tenant("t1", "w1", s1)
	h.mkWorkspace("t1", "w1", nil)
	lease := h.mustAcquire(tc, reuse())
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		_, err := h.worker.Exec(ctx, connect.NewRequest(&sandboxv1.ExecRequest{Tenant: tc, LeaseId: lease.GetLeaseId(), Argv: []string{"sh", "-c", "hang"}, TimeoutMs: 60_000}))
		done <- err
	}()
	time.Sleep(200 * time.Millisecond)
	cancel()
	if err := <-done; connect.CodeOf(err) != connect.CodeCanceled {
		t.Fatalf("取消的 Exec：%v", err)
	}
	// 服务那边处理取消（问 agent、记下没停）要一点时间；下一条命令排在它后面，看到的一定是记下之后的状态。
	_, err := h.exec(tc, lease, "echo restore")
	if e := connecterr.Decode(err); e == nil || e.Category != providererr.Conflict || e.Code != "exec_not_stopped" {
		t.Fatalf("还原检查点的命令：%v", err)
	}
	if _, err := h.release(tc, lease, true); err != nil {
		t.Fatal(err)
	}
	again := h.mustAcquire(tc, reuse())
	if again.GetLeaseId() != lease.GetLeaseId() {
		t.Fatalf("同一会话复用原来的环境：%s %s", again.GetLeaseId(), lease.GetLeaseId())
	}
	if out := h.mustExec(tc, again, "echo ok"); strings.TrimSpace(out) != "ok" {
		t.Fatalf("还掉之后照常执行：%q", out)
	}
}
