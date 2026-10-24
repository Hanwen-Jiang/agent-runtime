package server

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"os/exec"
	"strings"
)

// Git 在工作区里跑 git：不弹凭据提示，hooks 指向一个空目录（克隆来的仓库带的 hooks 不会执行）。
type Git struct {
	Bin      string
	HooksDir string
}

// Run 在 dir 里执行 git，返回标准输出；失败时错误里带标准错误的末尾。
func (g Git) Run(ctx context.Context, dir string, args ...string) (string, error) {
	return g.RunAs(ctx, dir, "", "", args...)
}

// RunAs 同 Run，提交时用给定的作者。
func (g Git) RunAs(ctx context.Context, dir, name, email string, args ...string) (string, error) {
	full := []string{"-c", "core.hooksPath=" + g.HooksDir, "-c", "core.autocrlf=false", "-c", "protocol.file.allow=never", "-c", "advice.detachedHead=false"}
	if name != "" {
		full = append(full, "-c", "user.name="+name, "-c", "user.email="+email)
	}
	full = append(full, args...)
	cmd := exec.CommandContext(ctx, g.Bin, full...)
	cmd.Dir = dir
	cmd.Env = append(os.Environ(), "GIT_TERMINAL_PROMPT=0", "GCM_INTERACTIVE=never", "GIT_ASKPASS=", "SSH_ASKPASS=")
	var stdout, stderr bytes.Buffer
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	if err := cmd.Run(); err != nil {
		msg := strings.TrimSpace(stderr.String())
		if len(msg) > 2000 {
			msg = msg[len(msg)-2000:]
		}
		return stdout.String(), fmt.Errorf("git %s 失败：%v %s", args[0], err, msg)
	}
	return stdout.String(), nil
}

// Head 返回当前 commit；还没有提交时返回空。
func (g Git) Head(ctx context.Context, dir string) string {
	out, err := g.Run(ctx, dir, "rev-parse", "HEAD")
	if err != nil {
		return ""
	}
	return strings.TrimSpace(out)
}
