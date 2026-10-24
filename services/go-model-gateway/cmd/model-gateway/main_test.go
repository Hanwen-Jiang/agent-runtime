package main

// 交付“命令”与约定 4：serve 起来后 /healthz 返回 200，同一端口用 gRPC（明文 HTTP/2）调用 ModelGatewayService；
// 每次调用向标准输出写一行计量记录；dev-secret 从标准输入读密钥写入 AR_SECRETS_FILE 的平台作用域，密钥不经过命令行参数，
// 也不出现在标准输出与标准错误里；配置缺必填项时报错。供应商用 mock（libs/go-core/dev/mockmodel），全部离线。
// testdata/mock-profiles.json 的单价是为了让 mock 的少量 token 也能算出非零成本而编的，不是真实价格。

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"connectrpc.com/connect"

	"github.com/agentruntime/agent-runtime/libs/go-core/dev/filesecrets"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	mg "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/modelgateway/v1"
	"github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/modelgateway/v1/modelgatewayv1connect"
)

// leakyKey 是测试用的假密钥，用来断言它不出现在输出里。
const leakyKey = "sk-test-DO-NOT-LEAK-0123"

const keyName = "model-provider/api-key"

func getenv(m map[string]string) func(string) string { return func(k string) string { return m[k] } }

// syncBuffer 是并发安全的缓冲区（serve 在别的 goroutine 里写）。
type syncBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *syncBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *syncBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

// freeAddr 找一个空闲的本地端口。
func freeAddr(t *testing.T) string {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := ln.Addr().String()
	_ = ln.Close()
	return addr
}

// readSecret 用 filesecrets 读回平台作用域的密钥。
func readSecret(t *testing.T, path, name string) (string, error) {
	t.Helper()
	p, err := filesecrets.New(filesecrets.Options{Path: path})
	if err != nil {
		return "", err
	}
	store, err := p.BindPlatform("test")
	if err != nil {
		return "", err
	}
	v, err := store.Read(context.Background(), name)
	if err != nil {
		return "", err
	}
	return v.RevealString(), nil
}

type served struct {
	addr           string
	stdout, stderr *syncBuffer
	client         modelgatewayv1connect.ModelGatewayServiceClient
}

// startServe 在后台运行 serve，等到 /healthz 返回 200；测试结束时取消 ctx 并确认 serve 以 0 退出。
func startServe(t *testing.T, env map[string]string) *served {
	t.Helper()
	s := &served{addr: freeAddr(t), stdout: &syncBuffer{}, stderr: &syncBuffer{}}
	env["AR_MODEL_GATEWAY_ADDR"] = s.addr
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan int, 1)
	go func() { done <- run(ctx, []string{"serve"}, strings.NewReader(""), s.stdout, s.stderr, getenv(env)) }()
	t.Cleanup(func() {
		cancel()
		select {
		case code := <-done:
			if code != 0 {
				t.Errorf("serve 在 ctx 结束后应以 0 退出，实际 %d：%s", code, s.stderr.String())
			}
		case <-time.After(10 * time.Second):
			t.Error("serve 没有在 ctx 结束后退出")
		}
	})

	deadline := time.Now().Add(10 * time.Second)
	for {
		select {
		case code := <-done:
			done <- code
			t.Fatalf("serve 提前退出，退出码 %d：%s", code, s.stderr.String())
		default:
		}
		// 就绪检查用普通的 HTTP/1.1，与 Kubernetes 探针一样。
		resp, err := http.Get("http://" + s.addr + "/healthz")
		if err == nil {
			_ = resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				break
			}
		}
		if time.Now().After(deadline) {
			t.Fatalf("10 秒内 /healthz 没有返回 200：%v；stderr：%s", err, s.stderr.String())
		}
		time.Sleep(50 * time.Millisecond)
	}

	protocols := new(http.Protocols)
	protocols.SetUnencryptedHTTP2(true)
	httpClient := &http.Client{Transport: &http.Transport{Protocols: protocols}}
	t.Cleanup(httpClient.CloseIdleConnections)
	s.client = modelgatewayv1connect.NewModelGatewayServiceClient(httpClient, "http://"+s.addr, connect.WithGRPC())
	return s
}

func mockEnv(t *testing.T) map[string]string {
	t.Helper()
	secrets := filepath.Join(t.TempDir(), "secrets.json")
	// 密钥文件里放着可辨识的假密钥：即使用不到，也不能出现在任何输出里。
	p, err := filesecrets.New(filesecrets.Options{Path: secrets})
	if err != nil {
		t.Fatal(err)
	}
	p.SeedSecret("", keyName, leakyKey)
	return map[string]string{
		"AR_MODEL_PROFILES_FILE": filepath.Join("testdata", "mock-profiles.json"),
		"AR_MODEL_PROVIDER":      "mock",
		"AR_SECRETS_FILE":        secrets,
	}
}

func TestServeHealthzAndGRPCOnOnePort(t *testing.T) {
	s := startServe(t, mockEnv(t))
	ctx := context.Background()
	const prompt = "PROMPT-TEXT-NOT-FOR-METERING"

	resp, err := s.client.Generate(ctx, connect.NewRequest(&mg.GenerateRequest{
		Tenant:     &commonv1.TenantContext{TenantId: "t1", UserId: "u1"},
		Call:       &mg.CallContext{TaskId: "task-1", NodeId: "node-1", Role: commonv1.Role_ROLE_TRANSFORM},
		Generation: &mg.GenerateParams{Messages: []*mg.ChatMessage{{Role: mg.ChatRole_CHAT_ROLE_USER, Text: prompt}}},
	}))
	if err != nil {
		t.Fatalf("经 gRPC（h2c）调用 Generate：%v", err)
	}
	if m := resp.Msg.GetModel(); m.GetModelProfileId() != "mock/mid@1" || m.GetTier() != "mid@low" || m.GetVendor() != "mock" {
		t.Errorf("ModelChoice = %v", m)
	}
	if resp.Msg.GetText() != "mock reply: "+prompt {
		t.Errorf("text = %q", resp.Msg.GetText())
	}
	u := resp.Msg.GetUsage()
	if u.GetInputTokens() <= 0 || u.GetOutputTokens() <= 0 || u.GetCostUsdMicros() != u.GetInputTokens()*100+u.GetOutputTokens()*200 {
		t.Errorf("Usage 与按 mock/mid@1 单价（100/200）算的成本：%v", u)
	}

	emb, err := s.client.Embed(ctx, connect.NewRequest(&mg.EmbedRequest{
		Tenant: &commonv1.TenantContext{TenantId: "t1"}, Call: &mg.CallContext{TaskId: "task-1", NodeId: "node-2", Role: commonv1.Role_ROLE_FETCH},
		Texts: []string{"alpha", "beta"}, EmbeddingVersion: "mock/embed@1",
	}))
	if err != nil {
		t.Fatalf("Embed：%v", err)
	}
	if emb.Msg.GetEmbeddingVersion() != "mock/embed@1" || len(emb.Msg.GetEmbeddings()) != 2 {
		t.Errorf("Embed：%q，%d 个向量", emb.Msg.GetEmbeddingVersion(), len(emb.Msg.GetEmbeddings()))
	}

	// 缺租户：经真实服务也是 PermissionDenied（denied，与 tenancy 一致），并带 ar-error-bin。
	_, err = s.client.Generate(ctx, connect.NewRequest(&mg.GenerateRequest{
		Call: &mg.CallContext{Role: commonv1.Role_ROLE_TRANSFORM}, Generation: &mg.GenerateParams{Messages: []*mg.ChatMessage{{Role: mg.ChatRole_CHAT_ROLE_USER, Text: "hi"}}},
	}))
	if connect.CodeOf(err) != connect.CodePermissionDenied {
		t.Errorf("缺租户应是 PermissionDenied：%v", err)
	}
	var ce *connect.Error
	if !errors.As(err, &ce) || len(ce.Meta().Values("ar-error-bin")) == 0 {
		t.Errorf("错误应带 ar-error-bin trailer：%v", err)
	}

	// 计量：每次成功的调用一行 JSON，写在标准输出。
	var lines []map[string]any
	deadline := time.Now().Add(5 * time.Second)
	for {
		lines = lines[:0]
		for _, line := range strings.Split(s.stdout.String(), "\n") {
			if strings.TrimSpace(line) == "" {
				continue
			}
			var m map[string]any
			if err := json.Unmarshal([]byte(line), &m); err != nil {
				t.Fatalf("标准输出的每一行都应是计量 JSON：%q", line)
			}
			lines = append(lines, m)
		}
		if len(lines) >= 2 || time.Now().After(deadline) {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if len(lines) != 2 {
		t.Fatalf("两次成功的调用应有两行计量，实际 %d：%s", len(lines), s.stdout.String())
	}
	if lines[0]["tenant_id"] != "t1" || lines[0]["task_id"] != "task-1" || lines[0]["model_profile_id"] != "mock/mid@1" ||
		lines[0]["cost_usd_micros"] != float64(u.GetCostUsdMicros()) {
		t.Errorf("计量记录：%v", lines[0])
	}
	if lines[1]["model_profile_id"] != "mock/embed@1" || lines[1]["tier"] != "embed@default" {
		t.Errorf("嵌入的计量记录：%v", lines[1])
	}
	for where, text := range map[string]string{"标准输出": s.stdout.String(), "标准错误": s.stderr.String()} {
		if strings.Contains(text, leakyKey) {
			t.Errorf("密钥出现在%s里", where)
		}
		if strings.Contains(text, prompt) {
			t.Errorf("消息内容出现在%s里", where)
		}
	}
}

func TestServeRejectsBadConfig(t *testing.T) {
	cases := map[string]struct {
		env     func(m map[string]string)
		code    int
		mention string
	}{
		"缺 Profile 文件":      {func(m map[string]string) { delete(m, "AR_MODEL_PROFILES_FILE") }, 2, "AR_MODEL_PROFILES_FILE"},
		"未知的供应商":            {func(m map[string]string) { m["AR_MODEL_PROVIDER"] = "telepathy" }, 2, "AR_MODEL_PROVIDER"},
		"openai_compat 缺端点": {func(m map[string]string) { m["AR_MODEL_PROVIDER"] = "openai_compat" }, 2, "AR_MODEL_PROVIDER_BASE_URL"},
		"Profile 文件不存在":     {func(m map[string]string) { m["AR_MODEL_PROFILES_FILE"] = filepath.Join(t.TempDir(), "missing.json") }, 1, ""},
		"Profile 文件不合法": {func(m map[string]string) {
			m["AR_MODEL_PROFILES_FILE"] = writeFile(t, `{"version":1,"profiles":[{"id":""}]}`)
		}, 1, ""},
	}
	for name, c := range cases {
		env := mockEnv(t)
		env["AR_MODEL_GATEWAY_ADDR"] = freeAddr(t)
		c.env(env)
		var stdout, stderr syncBuffer
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		code := run(ctx, []string{"serve"}, strings.NewReader(""), &stdout, &stderr, getenv(env))
		cancel()
		if code != c.code {
			t.Errorf("%s：退出码 %d，want %d；stderr：%s", name, code, c.code, stderr.String())
		}
		if c.mention != "" && !strings.Contains(stderr.String(), c.mention) {
			t.Errorf("%s：标准错误应指出 %s：%s", name, c.mention, stderr.String())
		}
	}
}

func writeFile(t *testing.T, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "file.json")
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestDevSecretReadsStdinAndWritesPlatformScope(t *testing.T) {
	path := filepath.Join(t.TempDir(), "secrets.json")
	// 文件里已有别的作用域的内容（task-service 与 Worker 共用这份文件）：不能被覆盖。
	p, err := filesecrets.New(filesecrets.Options{Path: path})
	if err != nil {
		t.Fatal(err)
	}
	p.SeedSecret("t1", "byok", "tenant-own-key")

	var stdout, stderr syncBuffer
	code := run(context.Background(), []string{"dev-secret", keyName}, strings.NewReader(leakyKey+"\r\n"), &stdout, &stderr,
		getenv(map[string]string{"AR_SECRETS_FILE": path}))
	if code != 0 {
		t.Fatalf("dev-secret 应成功，退出码 %d：%s", code, stderr.String())
	}
	if got, err := readSecret(t, path, keyName); err != nil || got != leakyKey {
		t.Errorf("平台作用域的 %s 应为标准输入的内容（去掉换行），实际 %q（%v）", keyName, got, err)
	}
	if strings.Contains(stdout.String(), leakyKey) || strings.Contains(stderr.String(), leakyKey) {
		t.Error("密钥不得出现在标准输出或标准错误里")
	}
	p, err = filesecrets.New(filesecrets.Options{Path: path})
	if err != nil {
		t.Fatal(err)
	}
	store, err := p.Bind(&commonv1.TenantContext{TenantId: "t1"})
	if err != nil {
		t.Fatal(err)
	}
	if v, err := store.Read(context.Background(), "byok"); err != nil || v.RevealString() != "tenant-own-key" {
		t.Errorf("别的作用域的密钥应保留：%v", err)
	}
	if v, err := store.Read(context.Background(), keyName); err == nil {
		t.Errorf("dev-secret 只写平台作用域，租户作用域不应读到：%v", v)
	}
}

func TestDevSecretUsageErrors(t *testing.T) {
	path := filepath.Join(t.TempDir(), "secrets.json")
	env := getenv(map[string]string{"AR_SECRETS_FILE": path})
	cases := map[string]struct {
		args  []string
		stdin string
		env   func(string) string
		code  int
	}{
		"缺名称":               {[]string{"dev-secret"}, leakyKey, env, 2},
		"多个名称":              {[]string{"dev-secret", "a", "b"}, leakyKey, env, 2},
		"缺 AR_SECRETS_FILE": {[]string{"dev-secret", keyName}, leakyKey, getenv(nil), 2},
		"标准输入为空":            {[]string{"dev-secret", keyName}, " \n", env, 1},
	}
	for name, c := range cases {
		var stdout, stderr syncBuffer
		if code := run(context.Background(), c.args, strings.NewReader(c.stdin), &stdout, &stderr, c.env); code != c.code {
			t.Errorf("%s：退出码 %d，want %d；stderr：%s", name, code, c.code, stderr.String())
		}
		if strings.Contains(stdout.String()+stderr.String(), leakyKey) {
			t.Errorf("%s：密钥不得出现在输出里", name)
		}
	}
	if _, err := readSecret(t, path, keyName); err == nil {
		t.Error("失败的 dev-secret 不应写入密钥")
	}
}

func TestUnknownSubcommandIsAUsageError(t *testing.T) {
	var stdout, stderr syncBuffer
	if code := run(context.Background(), []string{"frobnicate"}, strings.NewReader(""), &stdout, &stderr, getenv(nil)); code != 2 {
		t.Errorf("未知子命令应返回 2，实际 %d", code)
	}
	if code := run(context.Background(), nil, strings.NewReader(""), &stdout, &stderr, getenv(nil)); code != 2 {
		t.Errorf("没有子命令应返回 2，实际 %d", code)
	}
}
