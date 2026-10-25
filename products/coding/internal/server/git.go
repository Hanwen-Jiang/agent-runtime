package server

import (
	"bytes"
	"context"
	"encoding/base64"
	"fmt"
	"os"
	"os/exec"
	"strings"
)

// Git 在工作区里跑 git：不弹凭据提示，hooks 指向一个空目录（克隆来的仓库带的 hooks 不会执行），不接受 file:// 远端。
type Git struct {
	Bin      string
	HooksDir string
	// AllowFileProtocol 只给测试用：允许本地路径作远端（测试里的“GitHub”是一个本地裸仓库）。
	AllowFileProtocol bool
}

type gitOpts struct {
	name, email string
	// env 追加到 git 进程的环境里；用来传 http.extraheader（令牌不出现在命令行参数、Git 配置与远端地址里）。
	env []string
}

// Run 在 dir 里执行 git，返回标准输出；失败时错误里带标准错误的末尾。
func (g Git) Run(ctx context.Context, dir string, args ...string) (string, error) {
	return g.run(ctx, dir, gitOpts{}, args...)
}

// RunAs 同 Run，提交时用给定的作者。
func (g Git) RunAs(ctx context.Context, dir, name, email string, args ...string) (string, error) {
	return g.run(ctx, dir, gitOpts{name: name, email: email}, args...)
}

// RunWithToken 同 Run，对 host（例如 https://github.com/）的请求带上 OAuth 令牌。
func (g Git) RunWithToken(ctx context.Context, dir, host, token string, args ...string) (string, error) {
	return g.run(ctx, dir, gitOpts{env: tokenEnv(host, token)}, args...)
}

// tokenEnv 用 GIT_CONFIG_COUNT 把 http.<host>.extraheader 放进环境变量：只对这个 host 生效，不落盘。
func tokenEnv(host, token string) []string {
	if token == "" {
		return nil
	}
	basic := base64.StdEncoding.EncodeToString([]byte(token + ":x-oauth-basic"))
	return []string{
		"GIT_CONFIG_COUNT=1",
		"GIT_CONFIG_KEY_0=http." + strings.TrimRight(host, "/") + "/.extraheader",
		"GIT_CONFIG_VALUE_0=AUTHORIZATION: basic " + basic,
	}
}

func (g Git) run(ctx context.Context, dir string, o gitOpts, args ...string) (string, error) {
	file := "never"
	if g.AllowFileProtocol {
		file = "always"
	}
	full := []string{"-c", "core.hooksPath=" + g.HooksDir, "-c", "core.autocrlf=false", "-c", "protocol.file.allow=" + file, "-c", "advice.detachedHead=false"}
	if o.name != "" {
		full = append(full, "-c", "user.name="+o.name, "-c", "user.email="+o.email)
	}
	full = append(full, args...)
	cmd := exec.CommandContext(ctx, g.Bin, full...)
	cmd.Dir = dir
	cmd.Env = append(os.Environ(), "GIT_TERMINAL_PROMPT=0", "GCM_INTERACTIVE=never", "GIT_ASKPASS=", "SSH_ASKPASS=")
	cmd.Env = append(cmd.Env, o.env...)
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

// Branch 返回当前分支名；分离头指针时返回空。
func (g Git) Branch(ctx context.Context, dir string) string {
	out, err := g.Run(ctx, dir, "symbolic-ref", "--quiet", "--short", "HEAD")
	if err != nil {
		return ""
	}
	return strings.TrimSpace(out)
}
