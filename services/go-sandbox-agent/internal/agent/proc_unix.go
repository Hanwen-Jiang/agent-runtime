//go:build !windows

package agent

import (
	"os"
	"os/exec"
	"syscall"
)

// setProcessGroup 让命令自成一个进程组：超时时连同它起的子进程一起杀掉。
func setProcessGroup(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
}

func killGroup(cmd *exec.Cmd) {
	if cmd.Process == nil {
		return
	}
	_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
	_ = cmd.Process.Kill()
}

// exitCode 与 Python 的 returncode 一致：被信号杀掉时是负的信号编号。
func exitCode(ps *os.ProcessState) int {
	if ws, ok := ps.Sys().(syscall.WaitStatus); ok && ws.Signaled() {
		return -int(ws.Signal())
	}
	return ps.ExitCode()
}
