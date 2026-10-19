package session

// S-03 验收：重放（计划 S-03 验收“重放”，硬门槛）。
//   - testdata/histories 里是集成测试导出的 Session 工作流事件历史（tests/e2e 导出，见 CLAUDE.md 常用命令），
//     当前代码必须都能回放；回放用的参数故意与录制时不同，证明参数经 SideEffect 记入历史、不影响重放。
//   - 把工作流里两条标了 replay-mutation 的语句调换顺序（发 TASK_ACCEPTED 与启动子工作流），
//     重放必须失败，证明重放测试确实能发现命令顺序的改变。

import (
	"bytes"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"testing"

	"go.temporal.io/sdk/worker"
	"go.temporal.io/sdk/workflow"
)

const historiesGlob = "testdata/histories/*.json"

var (
	continuedAsNew  = regexp.MustCompile(`(?i)continued_?as_?new`)
	cancelRequested = regexp.MustCompile(`(?i)request_?cancel_?external`)
)

func goldenHistories(t *testing.T) []string {
	t.Helper()
	files, err := filepath.Glob(historiesGlob)
	if err != nil {
		t.Fatal(err)
	}
	if len(files) < 3 {
		t.Fatalf("%s 下至少要有 3 份事件历史（普通、续接、取消），实际 %d 份；用 tests/e2e 导出", historiesGlob, len(files))
	}
	return files
}

func TestGoldenHistoriesCoverContinueAsNewAndCancel(t *testing.T) {
	var sawCAN, sawCancel bool
	for _, f := range goldenHistories(t) {
		data, err := os.ReadFile(f)
		if err != nil {
			t.Fatal(err)
		}
		sawCAN = sawCAN || continuedAsNew.Match(data)
		sawCancel = sawCancel || cancelRequested.Match(data)
	}
	if !sawCAN {
		t.Error("事件历史里没有 Continue-As-New 的运行")
	}
	if !sawCancel {
		t.Error("事件历史里没有取消运行中 Task 的运行")
	}
}

func TestReplayGoldenHistories(t *testing.T) {
	cfg := DefaultConfig()
	cfg.ReadOnlyLimit = 97 // 与录制时不同
	cfg.ContinueAsNewTasks = 0
	r := worker.NewWorkflowReplayer()
	r.RegisterWorkflowWithOptions(NewWorkflow(cfg), workflow.RegisterOptions{Name: WorkflowName})
	for _, f := range goldenHistories(t) {
		t.Run(filepath.Base(f), func(t *testing.T) {
			if err := r.ReplayWorkflowHistoryFromJSONFile(nil, f); err != nil {
				t.Fatalf("当前代码无法回放 %s：%v", f, err)
			}
		})
	}
}

const (
	mutationA = "// replay-mutation:A"
	mutationB = "// replay-mutation:B"
)

// swapMarkedLines 调换各带一个标记的两行。
func swapMarkedLines(src []byte) ([]byte, error) {
	lines := bytes.Split(src, []byte("\n"))
	a, b := -1, -1
	for i, l := range lines {
		if bytes.Contains(l, []byte(mutationA)) {
			if a >= 0 {
				return nil, errDuplicateMarker(mutationA)
			}
			a = i
		}
		if bytes.Contains(l, []byte(mutationB)) {
			if b >= 0 {
				return nil, errDuplicateMarker(mutationB)
			}
			b = i
		}
	}
	if a < 0 || b < 0 {
		return nil, errMissingMarker{}
	}
	lines[a], lines[b] = lines[b], lines[a]
	return bytes.Join(lines, []byte("\n")), nil
}

type errDuplicateMarker string

func (e errDuplicateMarker) Error() string { return "标记出现了不止一次：" + string(e) }

type errMissingMarker struct{}

func (errMissingMarker) Error() string {
	return "workflow.go 里找不到 " + mutationA + " 与 " + mutationB + " 两个标记"
}

func TestReplayDetectsReorderedCommands(t *testing.T) {
	if testing.Short() {
		t.Skip("要复制模块并重新编译")
	}
	goldenHistories(t)
	moduleDir, err := filepath.Abs(filepath.Join("..", ".."))
	if err != nil {
		t.Fatal(err)
	}
	repo := filepath.Join(moduleDir, "..", "..")
	tmp := t.TempDir()
	copyDir(t, moduleDir, filepath.Join(tmp, "go-task-service"))

	target := filepath.Join(tmp, "go-task-service", "internal", "session", "workflow.go")
	src, err := os.ReadFile(target)
	if err != nil {
		t.Fatal(err)
	}
	mutated, err := swapMarkedLines(src)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(target, mutated, 0o644); err != nil {
		t.Fatal(err)
	}

	// 复制出来的模块里 go.mod 的相对 replace 失效，用 go.work 的 replace 指回仓库里的模块。
	// 路径加引号：仓库在共享盘上时是 \\host\share 形式，不加引号的 // 会被当成注释。
	work := "go 1.26.0\n\nuse ./go-task-service\n\nreplace (\n" +
		"\tgithub.com/agentruntime/agent-runtime/libs/go-core => " + strconv.Quote(filepath.Join(repo, "libs", "go-core")) + "\n" +
		"\tgithub.com/agentruntime/agent-runtime/proto/gen/go => " + strconv.Quote(filepath.Join(repo, "proto", "gen", "go")) + "\n" +
		")\n"
	if err := os.WriteFile(filepath.Join(tmp, "go.work"), []byte(work), 0o644); err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command("go", "test", "-count=1", "-run", "^TestReplayGoldenHistories$", "./internal/session/")
	cmd.Dir = filepath.Join(tmp, "go-task-service")
	cmd.Env = append(os.Environ(), "GOWORK="+filepath.Join(tmp, "go.work"), "GOFLAGS=")
	out, err := cmd.CombinedOutput()
	if err == nil {
		t.Fatalf("调换命令顺序后重放仍然通过，重放测试无效：\n%s", out)
	}
	// TMPRL1100 是 Temporal SDK 的不确定性错误码；命令对不上时 Go SDK 的说明里未必出现 nondeterministic 字样。
	if !regexp.MustCompile(`(?i)nondetermin|TMPRL1100`).Match(out) {
		t.Fatalf("重放失败了，但不是因为不确定性错误：\n%s", out)
	}
}

func copyDir(t *testing.T, src, dst string) {
	t.Helper()
	err := filepath.WalkDir(src, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(src, path)
		if d.IsDir() {
			return os.MkdirAll(filepath.Join(dst, rel), 0o755)
		}
		if strings.HasSuffix(path, ".exe") {
			return nil
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		return os.WriteFile(filepath.Join(dst, rel), data, 0o644)
	})
	if err != nil {
		t.Fatal(err)
	}
}
