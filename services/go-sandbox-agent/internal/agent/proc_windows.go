//go:build windows

package agent

import (
	"os"
	"os/exec"
	"strconv"
)

// Windows 上只为本机跑测试：没有进程组，用 taskkill /T 杀命令和它的子进程（WaitDelay 兜底不让收尾卡住）。
func setProcessGroup(*exec.Cmd) {}

func killGroup(cmd *exec.Cmd) {
	if cmd.Process != nil {
		_ = exec.Command("taskkill", "/T", "/F", "/PID", strconv.Itoa(cmd.Process.Pid)).Run()
		_ = cmd.Process.Kill()
	}
}

func exitCode(ps *os.ProcessState) int { return ps.ExitCode() }
