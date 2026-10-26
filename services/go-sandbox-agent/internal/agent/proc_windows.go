//go:build windows

package agent

import (
	"os"
	"os/exec"
)

// Windows 上只为本机跑测试：没有进程组，超时只杀命令本身（WaitDelay 兜底不让收尾卡住）。
func setProcessGroup(*exec.Cmd) {}

func killGroup(cmd *exec.Cmd) {
	if cmd.Process != nil {
		_ = cmd.Process.Kill()
	}
}

func exitCode(ps *os.ProcessState) int { return ps.ExitCode() }
