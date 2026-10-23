package main

// S-03 验收：命令行入口。dev-token 签发的令牌能通过内置 Ingress 的认证（端到端测试用它）；
// replay 子命令就是重放测试工具（计划 S-03 交付“重放测试工具”），回放成功返回 0，失败返回 1。

import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/agentruntime/agent-runtime/libs/go-core/dev/localingress"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/ingress"
)

const signingKey = "cmd-test-signing-key"

func getenv(m map[string]string) func(string) string { return func(k string) string { return m[k] } }

func TestDevTokenIsAcceptedByTheIngress(t *testing.T) {
	var out, errOut bytes.Buffer
	code := run([]string{"dev-token", "--tenant", "t1", "--user", "u1", "--ttl", "1h"}, &out, &errOut,
		getenv(map[string]string{"AR_DEV_SIGNING_KEY": signingKey}))
	if code != 0 {
		t.Fatalf("dev-token 应成功，退出码 %d：%s", code, errOut.String())
	}
	token := strings.TrimSpace(out.String())
	id, err := localingress.New(localingress.Options{SigningKey: []byte(signingKey)}).
		Authenticate(context.Background(), ingress.Credentials{BearerToken: token})
	if err != nil || id.TenantID != "t1" || id.UserID != "u1" {
		t.Errorf("令牌应能通过认证并带出 t1/u1，实际 %+v（%v）", id, err)
	}
}

func TestDevTokenCarriesScopes(t *testing.T) {
	var out, errOut bytes.Buffer
	code := run([]string{"dev-token", "--tenant", "t1", "--user", "u3", "--scope", "task.approve"}, &out, &errOut,
		getenv(map[string]string{"AR_DEV_SIGNING_KEY": signingKey}))
	if code != 0 {
		t.Fatalf("dev-token 应成功，退出码 %d：%s", code, errOut.String())
	}
	id, err := localingress.New(localingress.Options{SigningKey: []byte(signingKey)}).
		Authenticate(context.Background(), ingress.Credentials{BearerToken: strings.TrimSpace(out.String())})
	if err != nil || len(id.Scopes) != 1 || id.Scopes[0] != "task.approve" {
		t.Errorf("令牌应带出 scope task.approve（R-05 评审 D9），实际 %+v（%v）", id, err)
	}
}

func TestDevTokenNeedsTheSigningKey(t *testing.T) {
	var out, errOut bytes.Buffer
	if code := run([]string{"dev-token", "--tenant", "t1", "--user", "u1"}, &out, &errOut, getenv(nil)); code == 0 {
		t.Error("没有 AR_DEV_SIGNING_KEY 时应失败")
	}
}

func TestUnknownSubcommandIsAUsageError(t *testing.T) {
	var out, errOut bytes.Buffer
	if code := run([]string{"frobnicate"}, &out, &errOut, getenv(nil)); code != 2 {
		t.Errorf("未知子命令应返回 2，实际 %d", code)
	}
	if code := run(nil, &out, &errOut, getenv(nil)); code != 2 {
		t.Errorf("没有子命令应返回 2，实际 %d", code)
	}
}

func TestReplaySubcommand(t *testing.T) {
	golden := filepath.Join("..", "..", "internal", "session", "testdata", "histories")
	var out, errOut bytes.Buffer
	if code := run([]string{"replay", "--dir", golden}, &out, &errOut, getenv(nil)); code != 0 {
		t.Fatalf("当前代码应能回放 %s，退出码 %d：%s", golden, code, errOut.String())
	}

	empty := t.TempDir()
	if code := run([]string{"replay", "--dir", empty}, &out, &errOut, getenv(nil)); code == 0 {
		t.Error("目录里没有事件历史时应失败，免得误以为回放过了")
	}

	broken := t.TempDir()
	if err := os.WriteFile(filepath.Join(broken, "bad.json"), []byte(`{"events":[{"eventId":"1"}]}`), 0o644); err != nil {
		t.Fatal(err)
	}
	errOut.Reset()
	if code := run([]string{"replay", "--dir", broken}, &out, &errOut, getenv(nil)); code != 1 {
		t.Errorf("回放失败应返回 1，实际 %d", code)
	}
	if !strings.Contains(errOut.String(), "bad.json") {
		t.Errorf("失败信息应指出哪份历史回放失败：%s", errOut.String())
	}
}
