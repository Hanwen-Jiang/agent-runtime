package testkit

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
)

// ErrMissingTool 表示编译 sandbox-agent 要的工具（cargo 或 go）不在 PATH 上。
var ErrMissingTool = errors.New("testkit: 缺工具")

// LinuxMuslTarget 是装进 FROM scratch 镜像的 Linux 静态二进制的目标（与 py-core 的 agentbuild 相同）。
const LinuxMuslTarget = "x86_64-unknown-linux-musl"

var (
	agentOnce sync.Once
	agentPath string
	agentErr  error
)

// RepoRoot 是仓库根。
func RepoRoot() string {
	_, file, _, _ := runtime.Caller(0)
	return filepath.Join(filepath.Dir(file), "..", "..", "..")
}

// SandboxAgentImpl 是测试用哪一版 sandbox-agent（D-28）：默认 rust（services/rs-sandbox-agent），两版并存的一个发布周期内
// AR_SANDBOX_AGENT_IMPL=go 改用 Go 版（services/go-sandbox-agent）对照。
func SandboxAgentImpl() (string, error) {
	impl := strings.TrimSpace(os.Getenv("AR_SANDBOX_AGENT_IMPL"))
	if impl == "" {
		impl = "rust"
	}
	if impl != "rust" && impl != "go" {
		return "", fmt.Errorf("AR_SANDBOX_AGENT_IMPL 只能是 rust 或 go：%q", impl)
	}
	return impl, nil
}

// SandboxAgentBinary 编译本机跑的 sandbox-agent，同一个测试进程只编一次（与 py-core 的 agentbuild 同样的规矩：
// AR_SANDBOX_AGENT_BIN 给了就直接用；默认 Rust 版，AR_SANDBOX_AGENT_IMPL=go 时 Go 版）。沙箱后端的一致性测试与假桥都用真的 agent。
func SandboxAgentBinary() (string, error) {
	agentOnce.Do(func() {
		if given := os.Getenv("AR_SANDBOX_AGENT_BIN"); given != "" {
			agentPath = given
			return
		}
		dir, err := os.MkdirTemp("", "ar-sandbox-agent-")
		if err != nil {
			agentErr = err
			return
		}
		agentPath, agentErr = BuildSandboxAgent(dir, false)
	})
	return agentPath, agentErr
}

// BuildSandboxAgent 把 sandbox-agent 编译到 dir/sandbox-agent。linuxStatic 为真时编译 Linux 的静态二进制（kind 的镜像用）。
func BuildSandboxAgent(dir string, linuxStatic bool) (string, error) {
	impl, err := SandboxAgentImpl()
	if err != nil {
		return "", err
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", err
	}
	dest := filepath.Join(dir, "sandbox-agent")
	if impl == "go" {
		if _, err := exec.LookPath("go"); err != nil {
			return "", fmt.Errorf("%w：找不到 go（AR_SANDBOX_AGENT_IMPL=go 时要编译 Go 版 sandbox-agent）", ErrMissingTool)
		}
		cmd := exec.Command("go", "build", "-trimpath", "-o", dest, "./cmd/sandbox-agent")
		cmd.Dir = filepath.Join(RepoRoot(), "services", "go-sandbox-agent")
		cmd.Env = os.Environ()
		if linuxStatic {
			cmd.Env = append(cmd.Env, "GOOS=linux", "GOARCH=amd64", "CGO_ENABLED=0")
		}
		if out, err := cmd.CombinedOutput(); err != nil {
			return "", fmt.Errorf("编译 sandbox-agent 失败：%v\n%s", err, out)
		}
		return dest, nil
	}
	if _, err := exec.LookPath("cargo"); err != nil {
		return "", fmt.Errorf("%w：找不到 cargo（要编译 sandbox-agent，Rust 工具链见 rust-toolchain.toml）", ErrMissingTool)
	}
	args := []string{"build", "--locked", "--release", "-p", "rs-sandbox-agent"}
	built := filepath.Join(RepoRoot(), "target", "release", "sandbox-agent")
	if linuxStatic {
		args = append(args, "--target", LinuxMuslTarget)
		built = filepath.Join(RepoRoot(), "target", LinuxMuslTarget, "release", "sandbox-agent")
	}
	cmd := exec.Command("cargo", args...)
	cmd.Dir = RepoRoot()
	if out, err := cmd.CombinedOutput(); err != nil {
		tail := string(out)
		if len(tail) > 4000 {
			tail = tail[len(tail)-4000:]
		}
		return "", fmt.Errorf("编译 sandbox-agent 失败：%v\n%s", err, tail)
	}
	data, err := os.ReadFile(built)
	if err != nil {
		return "", err
	}
	if err := os.WriteFile(dest, data, 0o755); err != nil {
		return "", err
	}
	return dest, nil
}
