//go:build linux

package main

import "syscall"

// hideFromCommands 让 agent 不可转储（PR_SET_DUMPABLE=0，AGENT-TOK-01）：/proc/<agent>/ 下的文件属主变成 root，
// 与 agent 同一个 uid 的命令读不到它的 environ（启动时的环境块，os.Unsetenv 改不到，令牌就在里面）与 mem，也 ptrace 不上。
// 命令 exec 之后内核把它恢复成可转储，命令自己的 /proc/self 不受影响。代价：对 agent 的调试与 core dump 也被挡住。
func hideFromCommands() error {
	if _, _, errno := syscall.RawSyscall(syscall.SYS_PRCTL, syscall.PR_SET_DUMPABLE, 0, 0); errno != 0 {
		return errno
	}
	return nil
}
