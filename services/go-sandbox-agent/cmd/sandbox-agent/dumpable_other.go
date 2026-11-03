//go:build !linux

package main

// hideFromCommands 只在 Linux 上有事可做：沙箱都是 Linux，别的系统上的 agent 只给本机测试用。
func hideFromCommands() error { return nil }
