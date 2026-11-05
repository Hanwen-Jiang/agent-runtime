package testkit

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sync"
)

var (
	agentOnce sync.Once
	agentPath string
	agentErr  error
)

// SandboxAgentBinary 编译仓库里的 sandbox-agent（services/go-sandbox-agent），同一个测试进程只编一次。沙箱后端的一致性测试
// 与假桥都用真的 agent。要 PATH 上的 go。
func SandboxAgentBinary() (string, error) {
	agentOnce.Do(func() {
		_, file, _, _ := runtime.Caller(0)
		src := filepath.Join(filepath.Dir(file), "..", "..", "..", "services", "go-sandbox-agent")
		dir, err := os.MkdirTemp("", "ar-sandbox-agent-")
		if err != nil {
			agentErr = err
			return
		}
		agentPath = filepath.Join(dir, "sandbox-agent")
		cmd := exec.Command("go", "build", "-o", agentPath, "./cmd/sandbox-agent")
		cmd.Dir = src
		if out, err := cmd.CombinedOutput(); err != nil {
			agentErr = fmt.Errorf("编译 sandbox-agent 失败：%v\n%s", err, out)
		}
	})
	return agentPath, agentErr
}
