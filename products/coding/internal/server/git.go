package server

import (
	"bytes"
	"context"
	"encoding/base64"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
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
	// allowFile 只给 CloneLocal：本机路径作远端（两端路径都由服务端拼出）。
	allowFile bool
}

// Run 在 dir 里执行 git，返回标准输出；失败时错误里带标准错误的末尾。
func (g Git) Run(ctx context.Context, dir string, args ...string) (string, error) {
	return g.run(ctx, dir, gitOpts{}, args...)
}

// RunAs 同 Run，提交时用给定的作者。
func (g Git) RunAs(ctx context.Context, dir, name, email string, args ...string) (string, error) {
	return g.run(ctx, dir, gitOpts{name: name, email: email}, args...)
}

// RunWithAuth 同 Run，对 host（例如 https://github.com/）的请求带上 Basic 认证（各家平台的用户名与令牌见 Provider.gitAuth）。
func (g Git) RunWithAuth(ctx context.Context, dir, host, user, pass string, args ...string) (string, error) {
	return g.run(ctx, dir, gitOpts{env: basicEnv(host, user, pass)}, args...)
}

// basicEnv 用 GIT_CONFIG_COUNT 把 http.<host>.extraheader 放进环境变量：只对这个 host 生效，不落盘。
func basicEnv(host, user, pass string) []string {
	if host == "" || (user == "" && pass == "") {
		return nil
	}
	basic := base64.StdEncoding.EncodeToString([]byte(user + ":" + pass))
	return []string{
		"GIT_CONFIG_COUNT=1",
		"GIT_CONFIG_KEY_0=http." + strings.TrimRight(host, "/") + "/.extraheader",
		"GIT_CONFIG_VALUE_0=AUTHORIZATION: basic " + basic,
	}
}

// CloneLocal 把本机的仓库 src 克隆到 dst（对象用硬链接，得到的是一个完整、独立的仓库），检出 branch（为空时用 src 的 HEAD）。
func (g Git) CloneLocal(ctx context.Context, src, dst, branch string) (string, error) {
	args := []string{"clone", "-q", "--local"}
	if branch != "" {
		args = append(args, "--branch", branch)
	}
	out, err := g.run(ctx, filepath.Dir(dst), gitOpts{allowFile: true}, append(args, "--", src, dst)...)
	if err != nil && branch != "" { // src 里没有这个分支（例如还没有提交）：退回 HEAD
		_ = os.RemoveAll(dst)
		return g.run(ctx, filepath.Dir(dst), gitOpts{allowFile: true}, "clone", "-q", "--local", "--", src, dst)
	}
	return out, err
}

func (g Git) run(ctx context.Context, dir string, o gitOpts, args ...string) (string, error) {
	file := "never"
	if g.AllowFileProtocol || o.allowFile {
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
		return stdout.String(), fmt.Errorf("git %s: %v %s", args[0], err, msg) // git 自己的输出原样带上，不翻译
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
