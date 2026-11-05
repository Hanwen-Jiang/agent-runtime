// Package sandboxtest 是 Sandbox 的一致性测试（与 Python 侧 agentruntime_core.conformance.sandbox 一一对应；SBX-01 之后
// 本地、K8s、Cloudflare 三种后端都在 Go 里，跑这一份）。语义见 providers/sandbox。
//
// 三组：通用的（Run，所有实现）、绑定工作区的（Harness.WorkspacePath 不为 nil 时）、存档与浏览的（绑定得到的客户端实现
// sandbox.Archive 与 sandbox.Browse 时）。网络隔离与资源上限不在这里：各后端自己的测试覆盖。
// 命令用 python：本地实现映射到宿主的解释器，K8s 与 Cloudflare 的镜像里有 python。
package sandboxtest

import (
	"archive/tar"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/conformance"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/sandbox"
)

// Harness 由实现所在包提供。
type Harness interface {
	Provider() sandbox.Provider
	conformance.Faultable
	// WorkspacePath 是宿主一侧看到的工作区目录（<工作区根>/<tenant>/<workspace>）；不支持绑定工作区的实现返回 ""。
	WorkspacePath(tenantID, workspaceID string) string
}

const timeout = 30 * time.Second

// beat：命令和它的子进程各往一个文件里不停地追加，文件不再变长说明进程停了；30 秒后自己退出，取消没杀掉时不留在机器上。
const beat = `
import sys, time
end = time.monotonic() + 30
while time.monotonic() < end:
    with open(sys.argv[1], "a") as f:
        f.write(".")
    time.sleep(0.05)
`

var tree = fmt.Sprintf(`
import subprocess, sys
BEAT = %q
subprocess.Popen([sys.executable, "-c", BEAT, "child.beat"])
sys.argv = ["", "parent.beat"]
exec(BEAT)
`, beat)

var malformedPaths = []string{"../escape.txt", "/etc/passwd", "a/../../escape.txt", `C:\Windows\x`, `a\b`, ""}

func ctxT(t *testing.T) context.Context {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
	t.Cleanup(cancel)
	return ctx
}

func create(t *testing.T, ctx context.Context, s sandbox.Sandbox, spec sandbox.Spec) string {
	t.Helper()
	env, err := s.Create(ctx, spec)
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	t.Cleanup(func() { _ = s.Destroy(context.Background(), env.ID) })
	return env.ID
}

func run(t *testing.T, ctx context.Context, s sandbox.Sandbox, id, code string, opts sandbox.ExecOptions) sandbox.ExecResult {
	t.Helper()
	if opts.Timeout == 0 {
		opts.Timeout = timeout
	}
	res, err := s.Exec(ctx, id, []string{"python", "-c", code}, opts)
	if err != nil {
		t.Fatalf("Exec: %v", err)
	}
	return res
}

func exitCode(r sandbox.ExecResult) int {
	if r.ExitCode == nil {
		return -1
	}
	return *r.ExitCode
}

// Run 跑完整的 Sandbox 一致性测试。
func Run(t *testing.T, newHarness func(t *testing.T) Harness) {
	envs := map[string]string{}
	probe := conformance.Probe[sandbox.Sandbox]{
		Operation: "create",
		Write: func(ctx context.Context, s sandbox.Sandbox, name string) error {
			env, err := s.Create(ctx, sandbox.Spec{})
			if err != nil {
				return err
			}
			envs[name] = env.ID
			return s.WriteFile(ctx, env.ID, "probe.txt", []byte(name))
		},
		Visible: func(ctx context.Context, s sandbox.Sandbox, name string) (bool, error) {
			data, err := s.ReadFile(ctx, envs[name], "probe.txt")
			if providererr.CategoryOf(err) == providererr.NotFound {
				return false, nil
			}
			return err == nil && string(data) == name, err
		},
	}
	binder := func(t *testing.T) conformance.Binder[sandbox.Sandbox] { return newHarness(t).Provider() }
	conformance.RunBindingRules(t, binder)
	conformance.RunIsolationRules(t, binder, probe)
	conformance.RunErrorRules(t, func(t *testing.T) (conformance.Binder[sandbox.Sandbox], conformance.Faultable) {
		h := newHarness(t)
		return h.Provider(), h
	}, probe)

	setup := func(t *testing.T) (Harness, sandbox.Sandbox, context.Context) {
		h := newHarness(t)
		return h, conformance.Must(h.Provider().Bind(conformance.TenantA())), ctxT(t)
	}

	conformance.Case(t, "ExecRunsCode", func(t *testing.T) {
		_, s, ctx := setup(t)
		id := create(t, ctx, s, sandbox.Spec{})
		res := run(t, ctx, s, id, "print(6 * 7)", sandbox.ExecOptions{})
		if exitCode(res) != 0 || res.TimedOut || strings.TrimSpace(string(res.Stdout)) != "42" {
			t.Fatalf("res = %+v", res)
		}
	})
	conformance.Case(t, "ExecReturnsNonZeroExitCode", func(t *testing.T) {
		_, s, ctx := setup(t)
		id := create(t, ctx, s, sandbox.Spec{})
		res := run(t, ctx, s, id, "import sys; sys.stderr.write('bad'); sys.exit(3)", sandbox.ExecOptions{})
		if exitCode(res) != 3 || !bytes.Contains(res.Stderr, []byte("bad")) {
			t.Fatalf("res = %+v", res)
		}
	})
	conformance.Case(t, "ExecPassesStdin", func(t *testing.T) {
		_, s, ctx := setup(t)
		id := create(t, ctx, s, sandbox.Spec{})
		res := run(t, ctx, s, id, "import sys; print(sys.stdin.read().upper())", sandbox.ExecOptions{Stdin: []byte("abc")})
		if strings.TrimSpace(string(res.Stdout)) != "ABC" {
			t.Fatalf("stdout = %q", res.Stdout)
		}
	})
	conformance.Case(t, "ExecEnvIsOnlyForThatCommand", func(t *testing.T) {
		_, s, ctx := setup(t)
		id := create(t, ctx, s, sandbox.Spec{})
		code := "import os; print(os.environ.get('AR_TEST_EXEC_ENV', 'unset'))"
		if got := run(t, ctx, s, id, code, sandbox.ExecOptions{Env: map[string]string{"AR_TEST_EXEC_ENV": "given"}}); strings.TrimSpace(string(got.Stdout)) != "given" {
			t.Fatalf("stdout = %q", got.Stdout)
		}
		if got := run(t, ctx, s, id, code, sandbox.ExecOptions{}); strings.TrimSpace(string(got.Stdout)) != "unset" {
			t.Fatalf("env 只对那一条命令有效：stdout = %q", got.Stdout)
		}
		for _, bad := range []map[string]string{{"AGENT_TOKEN": "x"}, {"BAD-NAME": "x"}} {
			_, err := s.Exec(ctx, id, []string{"python", "-c", code}, sandbox.ExecOptions{Timeout: timeout, Env: bad})
			conformance.ExpectError(t, err, providererr.Invalid, "")
		}
	})
	conformance.Case(t, "ExecTimeoutIsReportedNotRaised", func(t *testing.T) {
		_, s, ctx := setup(t)
		id := create(t, ctx, s, sandbox.Spec{})
		res := run(t, ctx, s, id, "import time; time.sleep(30)", sandbox.ExecOptions{Timeout: 500 * time.Millisecond})
		if !res.TimedOut || res.ExitCode != nil {
			t.Fatalf("res = %+v", res)
		}
	})
	conformance.Case(t, "CancellingExecStopsTheCommandAndItsChildren", func(t *testing.T) {
		// 打断或取消 Task 时（docs/plan/running-task-interaction.md 第 2 节）调用方取消 ctx，命令要真的停，子进程也要停。
		_, s, ctx := setup(t)
		id := create(t, ctx, s, sandbox.Spec{})
		callCtx, cancel := context.WithCancel(ctx)
		done := make(chan error, 1)
		go func() {
			_, err := s.Exec(callCtx, id, []string{"python", "-c", tree}, sandbox.ExecOptions{Timeout: 60 * time.Second})
			done <- err
		}()
		beats := func(name string) int {
			data, err := s.ReadFile(ctx, id, name)
			if err != nil {
				return 0
			}
			return len(data)
		}
		started := false
		for range 400 {
			if beats("parent.beat") > 0 && beats("child.beat") > 0 {
				started = true
				break
			}
			select {
			case err := <-done:
				cancel()
				t.Fatalf("命令提前结束了：%v", err)
			default:
			}
			time.Sleep(50 * time.Millisecond)
		}
		if !started {
			cancel()
			t.Fatal("命令没有跑起来")
		}
		cancel()
		select {
		case err := <-done:
			if !errors.Is(err, context.Canceled) {
				t.Fatalf("取消之后 err = %v", err)
			}
		case <-time.After(20 * time.Second):
			t.Fatal("取消之后 Exec 没有返回")
		}
		time.Sleep(500 * time.Millisecond)
		p1, c1 := beats("parent.beat"), beats("child.beat")
		time.Sleep(time.Second)
		if p2, c2 := beats("parent.beat"), beats("child.beat"); p2 != p1 || c2 != c1 {
			t.Fatalf("取消之后还在跑：命令 %d→%d，子进程 %d→%d", p1, p2, c1, c2)
		}
	})
	conformance.Case(t, "FilesRoundtripAndAreVisibleToCode", func(t *testing.T) {
		_, s, ctx := setup(t)
		id := create(t, ctx, s, sandbox.Spec{})
		conformance.NoError(t, s.WriteFile(ctx, id, "data/input.txt", []byte("退款")))
		if got := conformance.Must(s.ReadFile(ctx, id, "data/input.txt")); string(got) != "退款" {
			t.Fatalf("read = %q", got)
		}
		code := "open('data/out.txt', 'w', encoding='utf-8').write(open('data/input.txt', encoding='utf-8').read() * 2)"
		if exitCode(run(t, ctx, s, id, code, sandbox.ExecOptions{})) != 0 {
			t.Fatal("代码没跑成")
		}
		if got := conformance.Must(s.ReadFile(ctx, id, "data/out.txt")); string(got) != "退款退款" {
			t.Fatalf("out = %q", got)
		}
	})
	conformance.Case(t, "EnvironmentsDoNotShareFiles", func(t *testing.T) {
		_, s, ctx := setup(t)
		first, second := create(t, ctx, s, sandbox.Spec{}), create(t, ctx, s, sandbox.Spec{})
		conformance.NoError(t, s.WriteFile(ctx, first, "only-here.txt", []byte("x")))
		_, err := s.ReadFile(ctx, second, "only-here.txt")
		conformance.ExpectError(t, err, providererr.NotFound, "")
	})
	conformance.Case(t, "PathsCannotEscapeWorkdir", func(t *testing.T) {
		_, s, ctx := setup(t)
		id := create(t, ctx, s, sandbox.Spec{})
		for _, path := range malformedPaths {
			conformance.ExpectError(t, s.WriteFile(ctx, id, path, []byte("x")), providererr.Invalid, "")
			_, err := s.ReadFile(ctx, id, path)
			conformance.ExpectError(t, err, providererr.Invalid, "")
		}
	})
	conformance.Case(t, "ReadMissingFileIsNotFound", func(t *testing.T) {
		_, s, ctx := setup(t)
		id := create(t, ctx, s, sandbox.Spec{})
		_, err := s.ReadFile(ctx, id, "missing.txt")
		conformance.ExpectError(t, err, providererr.NotFound, "")
	})
	conformance.Case(t, "DestroyedEnvironmentIsGone", func(t *testing.T) {
		_, s, ctx := setup(t)
		id := create(t, ctx, s, sandbox.Spec{})
		conformance.NoError(t, s.Destroy(ctx, id))
		_, err := s.Exec(ctx, id, []string{"python", "-c", "print(1)"}, sandbox.ExecOptions{Timeout: timeout})
		conformance.ExpectError(t, err, providererr.NotFound, "")
		conformance.NoError(t, s.Destroy(ctx, id))
	})
	conformance.Case(t, "OtherTenantCannotUseOrDestroyEnvironment", func(t *testing.T) {
		h, s, ctx := setup(t)
		id := create(t, ctx, s, sandbox.Spec{})
		b := conformance.Must(h.Provider().Bind(conformance.TenantB()))
		_, err := b.Exec(ctx, id, []string{"python", "-c", "print(1)"}, sandbox.ExecOptions{Timeout: timeout})
		conformance.ExpectError(t, err, providererr.NotFound, "")
		conformance.ExpectError(t, b.WriteFile(ctx, id, "x.txt", []byte("x")), providererr.NotFound, "")
		conformance.ExpectError(t, b.Destroy(ctx, id), providererr.NotFound, "")
		if exitCode(run(t, ctx, s, id, "print(1)", sandbox.ExecOptions{})) != 0 {
			t.Fatal("别的租户动不了，自己照常能用")
		}
	})
	conformance.Case(t, "UnknownEnvironmentIsNotFound", func(t *testing.T) {
		_, s, ctx := setup(t)
		_, err := s.Exec(ctx, "env-does-not-exist", []string{"python", "-c", "print(1)"}, sandbox.ExecOptions{Timeout: timeout})
		conformance.ExpectError(t, err, providererr.NotFound, "")
	})

	runWorkspace(t, newHarness)
	runArchive(t, newHarness)
}

// runWorkspace 是 Spec.WorkspaceID 的规则（挂卷，D-05）。目录由宿主一侧先建好（同 Sandbox Service 的做法）。
func runWorkspace(t *testing.T, newHarness func(t *testing.T) Harness) {
	setup := func(t *testing.T) (Harness, context.Context) {
		h := newHarness(t)
		if h.WorkspacePath(conformance.TenantAID, "w-probe") == "" {
			t.Skip("这个实现不绑定工作区（存档模式）")
		}
		return h, ctxT(t)
	}
	prepare := func(t *testing.T, h Harness, tenant, ws string) string {
		path := h.WorkspacePath(tenant, ws)
		conformance.NoError(t, os.MkdirAll(path, 0o755))
		return path
	}
	conformance.Case(t, "WorkspaceIsSharedWithTheHost", func(t *testing.T) {
		h, ctx := setup(t)
		s := conformance.Must(h.Provider().Bind(conformance.TenantA()))
		host := prepare(t, h, conformance.TenantAID, "w-share")
		conformance.NoError(t, os.WriteFile(filepath.Join(host, "seed.txt"), []byte("从宿主写入"), 0o644))
		id := create(t, ctx, s, sandbox.Spec{WorkspaceID: "w-share"})
		code := "open('made.txt', 'w', encoding='utf-8').write(open('seed.txt', encoding='utf-8').read() + '，沙箱读到了')"
		if exitCode(run(t, ctx, s, id, code, sandbox.ExecOptions{})) != 0 {
			t.Fatal("代码没跑成")
		}
		if got, _ := os.ReadFile(filepath.Join(host, "made.txt")); string(got) != "从宿主写入，沙箱读到了" {
			t.Fatalf("made.txt = %q", got)
		}
		if got := conformance.Must(s.ReadFile(ctx, id, "seed.txt")); string(got) != "从宿主写入" {
			t.Fatalf("seed.txt = %q", got)
		}
	})
	conformance.Case(t, "DestroyKeepsWorkspaceFiles", func(t *testing.T) {
		h, ctx := setup(t)
		s := conformance.Must(h.Provider().Bind(conformance.TenantA()))
		host := prepare(t, h, conformance.TenantAID, "w-keep")
		env := conformance.Must(s.Create(ctx, sandbox.Spec{WorkspaceID: "w-keep"}))
		conformance.NoError(t, s.WriteFile(ctx, env.ID, "kept.txt", []byte("x")))
		conformance.NoError(t, s.Destroy(ctx, env.ID))
		if got, _ := os.ReadFile(filepath.Join(host, "kept.txt")); string(got) != "x" {
			t.Fatalf("kept.txt = %q", got)
		}
	})
	conformance.Case(t, "SameWorkspaceIDDiffersPerTenant", func(t *testing.T) {
		h, ctx := setup(t)
		a := conformance.Must(h.Provider().Bind(conformance.TenantA()))
		b := conformance.Must(h.Provider().Bind(conformance.TenantB()))
		prepare(t, h, conformance.TenantAID, "w-same")
		prepare(t, h, conformance.TenantBID, "w-same")
		envA := create(t, ctx, a, sandbox.Spec{WorkspaceID: "w-same"})
		conformance.NoError(t, a.WriteFile(ctx, envA, "only-a.txt", []byte("a")))
		envB := create(t, ctx, b, sandbox.Spec{WorkspaceID: "w-same"})
		_, err := b.ReadFile(ctx, envB, "only-a.txt")
		conformance.ExpectError(t, err, providererr.NotFound, "")
	})
	conformance.Case(t, "PlatformScopeHasNoWorkspace", func(t *testing.T) {
		h, ctx := setup(t)
		p := conformance.Must(h.Provider().BindPlatform("conformance"))
		_, err := p.Create(ctx, sandbox.Spec{WorkspaceID: "w-1"})
		conformance.ExpectError(t, err, providererr.Invalid, "")
	})
	conformance.Case(t, "MalformedWorkspaceIDIsInvalid", func(t *testing.T) {
		h, ctx := setup(t)
		s := conformance.Must(h.Provider().Bind(conformance.TenantA()))
		for _, ws := range []string{"../w", "W-1", "w/1", "-w", strings.Repeat("w", 64)} {
			_, err := s.Create(ctx, sandbox.Spec{WorkspaceID: ws})
			conformance.ExpectError(t, err, providererr.Invalid, "")
		}
	})
}

type item struct{ name, kind, payload string }

// tarOf 是测试用的 tar：类型 file、dir、symlink、hardlink、char。
func tarOf(items ...item) []byte {
	var buf bytes.Buffer
	w := tar.NewWriter(&buf)
	for _, it := range items {
		hdr := &tar.Header{Name: it.name, Mode: 0o644, Format: tar.FormatPAX}
		switch it.kind {
		case "file":
			hdr.Typeflag, hdr.Size = tar.TypeReg, int64(len(it.payload))
		case "dir":
			hdr.Typeflag, hdr.Mode = tar.TypeDir, 0o755
		case "symlink":
			hdr.Typeflag, hdr.Linkname = tar.TypeSymlink, it.payload
		case "hardlink":
			hdr.Typeflag, hdr.Linkname = tar.TypeLink, it.payload
		case "char":
			hdr.Typeflag = tar.TypeChar
		}
		_ = w.WriteHeader(hdr)
		if it.kind == "file" {
			_, _ = w.Write([]byte(it.payload))
		}
	}
	_ = w.Close()
	return buf.Bytes()
}

// shape 比较两份清单用：路径、类型、大小、内容、链接（权限位不比）。
func shape(entries []sandbox.FileEntry) []string {
	out := make([]string, len(entries))
	for i, e := range entries {
		out[i] = fmt.Sprintf("%s|%s|%d|%s|%s", e.Path, e.Type, e.Size, e.SHA256, e.Link)
	}
	return out
}

var hostileArchives = map[string][]item{
	"absolute":      {{"/etc/passwd", "file", "x"}},
	"dotdot":        {{"../escape.txt", "file", "x"}},
	"nested-dotdot": {{"a/../../escape.txt", "file", "x"}},
	"backslash":     {{`a\b.txt`, "file", "x"}},
	"hardlink":      {{"a.txt", "file", "x"}, {"b.txt", "hardlink", "a.txt"}},
	"device":        {{"dev", "char", ""}},
	"escaping-link": {{"out", "symlink", "../../etc"}},
	"absolute-link": {{"out", "symlink", "/etc"}},
	"duplicate":     {{"a.txt", "file", "x"}, {"a.txt", "file", "y"}},
	"file-then-dir": {{"a", "file", "x"}, {"a/b.txt", "file", "y"}},
}

type archiveBox interface {
	sandbox.Sandbox
	sandbox.Archive
	sandbox.Browse
}

// runArchive 是存档模式（D-06）：sandbox.Archive 与 sandbox.Browse 的语义。
func runArchive(t *testing.T, newHarness func(t *testing.T) Harness) {
	setup := func(t *testing.T) (Harness, archiveBox, context.Context) {
		h := newHarness(t)
		s := conformance.Must(h.Provider().Bind(conformance.TenantA()))
		box, ok := s.(archiveBox)
		if !ok {
			t.Skip("这个实现不支持存档与浏览")
		}
		return h, box, ctxT(t)
	}
	importTar := func(ctx context.Context, box archiveBox, id string, data []byte, maxBytes int64) error {
		return box.ImportArchive(ctx, id, bytes.NewReader(data), int64(len(data)), maxBytes)
	}

	conformance.Case(t, "ExportThenImportIntoAFreshEnvironmentKeepsEverything", func(t *testing.T) {
		_, box, ctx := setup(t)
		src := create(t, ctx, box, sandbox.Spec{})
		files := map[string]string{"README.md": "# 退款\n", "src/app/main.py": "print('hi')\n", "empty.txt": "", ".git/HEAD": "ref: main\n", "a.txt": "a"}
		for path, data := range files {
			conformance.NoError(t, box.WriteFile(ctx, src, path, []byte(data)))
		}
		if exitCode(run(t, ctx, box, src, "import os; os.makedirs('empty/dir')", sandbox.ExecOptions{})) != 0 {
			t.Fatal("建目录没成")
		}
		var buf bytes.Buffer
		if n := conformance.Must(box.ExportArchive(ctx, src, &buf, 1<<20)); n <= 0 {
			t.Fatal("导出是空的")
		}
		dst := create(t, ctx, box, sandbox.Spec{})
		conformance.NoError(t, importTar(ctx, box, dst, buf.Bytes(), 1<<20))
		want, got := conformance.Must(box.Manifest(ctx, src)), conformance.Must(box.Manifest(ctx, dst))
		if !slices.Equal(shape(got), shape(want)) {
			t.Fatalf("清单不一样：\n%v\n%v", shape(got), shape(want))
		}
		sum := sha256.Sum256([]byte("# 退款\n"))
		paths := map[string]sandbox.FileEntry{}
		var order []string
		for _, e := range got {
			paths[e.Path] = e
			order = append(order, e.Path)
		}
		if r := paths["README.md"]; r.Type != "file" || r.Size != int64(len("# 退款\n")) || r.SHA256 != hex.EncodeToString(sum[:]) {
			t.Fatalf("README.md = %+v", r)
		}
		for _, p := range []string{"src", "src/app", "src/app/main.py", ".git", ".git/HEAD", "empty.txt", "empty", "empty/dir", "a.txt"} {
			if _, ok := paths[p]; !ok {
				t.Fatalf("清单里没有 %s", p)
			}
		}
		if !slices.IsSorted(order) {
			t.Fatalf("清单按路径的字符串顺序排序：%v", order)
		}
		if got := conformance.Must(box.ReadFile(ctx, dst, "src/app/main.py")); string(got) != "print('hi')\n" {
			t.Fatalf("main.py = %q", got)
		}
	})
	conformance.Case(t, "ImportNeedsAnEmptyWorkdirAndRespectsTheLimits", func(t *testing.T) {
		_, box, ctx := setup(t)
		id := create(t, ctx, box, sandbox.Spec{})
		conformance.NoError(t, box.WriteFile(ctx, id, "already.txt", []byte("x")))
		conformance.ExpectError(t, importTar(ctx, box, id, tarOf(item{"a.txt", "file", "a"}), 1024), providererr.Invalid, "not_empty")
		big := tarOf(item{"big.bin", "file", strings.Repeat("x", 4096)})
		conformance.ExpectError(t, importTar(ctx, box, create(t, ctx, box, sandbox.Spec{}), big, 1024), providererr.Invalid, "")
		conformance.NoError(t, box.WriteFile(ctx, id, "big.bin", bytes.Repeat([]byte("y"), 4096)))
		_, err := box.ExportArchive(ctx, id, &bytes.Buffer{}, 1024)
		conformance.ExpectError(t, err, providererr.Invalid, "too_large")
	})
	conformance.Case(t, "HostileArchivesAreRefused", func(t *testing.T) {
		_, box, ctx := setup(t)
		names := make([]string, 0, len(hostileArchives))
		for name := range hostileArchives {
			names = append(names, name)
		}
		slices.Sort(names)
		for _, name := range names {
			id := create(t, ctx, box, sandbox.Spec{})
			err := importTar(ctx, box, id, tarOf(hostileArchives[name]...), 1<<20)
			if providererr.CategoryOf(err) != providererr.Invalid {
				t.Fatalf("%s：err = %v，要 invalid", name, err)
			}
			for _, e := range conformance.Must(box.Manifest(ctx, id)) {
				if strings.HasSuffix(e.Path, "escape.txt") {
					t.Fatalf("%s：越界的项进了工作目录", name)
				}
			}
		}
	})
	conformance.Case(t, "SymlinksInsideTheWorkdirRoundtrip", func(t *testing.T) {
		_, box, ctx := setup(t)
		data := tarOf(item{"pkg", "dir", ""}, item{"pkg/real.py", "file", "x = 1\n"}, item{"pkg/alias.py", "symlink", "real.py"}, item{"top", "symlink", "pkg"})
		id := create(t, ctx, box, sandbox.Spec{})
		conformance.NoError(t, importTar(ctx, box, id, data, 1<<20))
		links := map[string]string{}
		for _, e := range conformance.Must(box.Manifest(ctx, id)) {
			if e.Type == "symlink" {
				links[e.Path] = e.Link
			}
		}
		if len(links) != 2 || links["pkg/alias.py"] != "real.py" || links["top"] != "pkg" {
			t.Fatalf("links = %v", links)
		}
		var buf bytes.Buffer
		conformance.Must(box.ExportArchive(ctx, id, &buf, 1<<20))
		again := create(t, ctx, box, sandbox.Spec{})
		conformance.NoError(t, importTar(ctx, box, again, buf.Bytes(), 1<<20))
		if !slices.Equal(shape(conformance.Must(box.Manifest(ctx, again))), shape(conformance.Must(box.Manifest(ctx, id)))) {
			t.Fatal("导出再导入之后清单不一样")
		}
	})
	conformance.Case(t, "OtherTenantsCannotExportImportOrBrowse", func(t *testing.T) {
		h, box, ctx := setup(t)
		id := create(t, ctx, box, sandbox.Spec{})
		conformance.NoError(t, box.WriteFile(ctx, id, "secret.txt", []byte("a")))
		other := conformance.Must(h.Provider().Bind(conformance.TenantB())).(archiveBox)
		_, err := other.ExportArchive(ctx, id, &bytes.Buffer{}, 1<<20)
		conformance.ExpectError(t, err, providererr.NotFound, "")
		conformance.ExpectError(t, importTar(ctx, other, id, tarOf(item{"x.txt", "file", "x"}), 1<<20), providererr.NotFound, "")
		_, err = other.Manifest(ctx, id)
		conformance.ExpectError(t, err, providererr.NotFound, "")
		_, err = other.ListDir(ctx, id, "")
		conformance.ExpectError(t, err, providererr.NotFound, "")
		_, _, err = other.Grep(ctx, id, "a", "", 100)
		conformance.ExpectError(t, err, providererr.NotFound, "")
	})
	conformance.Case(t, "ListAndGrepRunInsideTheEnvironment", func(t *testing.T) {
		_, box, ctx := setup(t)
		id := create(t, ctx, box, sandbox.Spec{})
		files := map[string]string{
			"a.py": "def refund():\n    return 1\n", "pkg/b.py": "x = 1\r\n# refund policy\r\n", "pkg/c.txt": "nothing\n",
			".git/config": "refund\n", "pkg/.git/x": "refund\n", "bin.dat": "refund\x00\x01",
		}
		for path, data := range files {
			conformance.NoError(t, box.WriteFile(ctx, id, path, []byte(data)))
		}
		var top []string
		for _, e := range conformance.Must(box.ListDir(ctx, id, "")) {
			top = append(top, e.Name+":"+e.Type)
		}
		if !slices.Equal(top, []string{".git:dir", "a.py:file", "bin.dat:file", "pkg:dir"}) {
			t.Fatalf("top = %v", top)
		}
		sub := conformance.Must(box.ListDir(ctx, id, "pkg"))
		if len(sub) != 3 || sub[0].Name != ".git" || sub[1].Name != "b.py" || sub[2].Name != "c.txt" || sub[1].Size != int64(len(files["pkg/b.py"])) {
			t.Fatalf("sub = %+v", sub)
		}
		_, err := box.ListDir(ctx, id, "missing")
		conformance.ExpectError(t, err, providererr.NotFound, "")
		_, err = box.ListDir(ctx, id, "../escape")
		conformance.ExpectError(t, err, providererr.Invalid, "")
		matches, truncated := conformance.Must2(box.Grep(ctx, id, "refund", "", 100))
		var got []string
		for _, m := range matches {
			got = append(got, fmt.Sprintf("%s:%d:%s", m.Path, m.Line, m.Text))
		}
		if !slices.Equal(got, []string{"a.py:1:def refund():", "pkg/b.py:2:# refund policy"}) || truncated {
			t.Fatalf("跳过 .git 与二进制：%v %v", got, truncated)
		}
		inPkg, _ := conformance.Must2(box.Grep(ctx, id, "refund", "pkg", 100))
		if len(inPkg) != 1 || inPkg[0].Path != "pkg/b.py" {
			t.Fatalf("pkg = %+v", inPkg)
		}
		if _, truncated := conformance.Must2(box.Grep(ctx, id, "re.und", "", 1)); !truncated {
			t.Fatal("最多 1 条时要标截断")
		}
		_, _, err = box.Grep(ctx, id, "(", "", 100)
		conformance.ExpectError(t, err, providererr.Invalid, "")
	})
}
