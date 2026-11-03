//go:build linux

package main

// AGENT-TOK-01（docs/plan/auth-tokens-2026-11-03.md）：agent 与它起的命令是同一个 uid，命令不能从 /proc 读到 agent 的令牌。
// os.Unsetenv 只改 Go 里的副本，/proc/<agent>/environ 读的是进程启动时的环境块，所以这里起一个真的 agent 进程：
// 测试二进制带上 SANDBOX_AGENT_TEST_RUN=1 就跑 main()；agent 再把测试二进制当命令起（proc-probe），从命令一侧读 agent 的 /proc。

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"strings"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/services/go-sandbox-agent/internal/agent"
)

const runAgentEnv = "SANDBOX_AGENT_TEST_RUN"

func TestMain(m *testing.M) {
	if os.Getenv(runAgentEnv) == "1" {
		main()
		return
	}
	if len(os.Args) > 1 && os.Args[1] == "proc-probe" {
		probe()
		return
	}
	os.Exit(m.Run())
}

// probeResult 是命令一侧看到的父进程（agent）的 /proc。
type probeResult struct {
	PID          int    `json:"pid"`
	Environ      string `json:"environ"`
	EnvironErr   string `json:"environ_err"`
	Cmdline      string `json:"cmdline"`
	MemOpened    bool   `json:"mem_opened"`
	SelfReadable bool   `json:"self_readable"`
}

func probe() {
	r := probeResult{PID: os.Getppid()}
	if data, err := os.ReadFile(fmt.Sprintf("/proc/%d/environ", r.PID)); err != nil {
		r.EnvironErr = err.Error()
	} else {
		r.Environ = string(data)
	}
	if data, err := os.ReadFile(fmt.Sprintf("/proc/%d/cmdline", r.PID)); err == nil {
		r.Cmdline = string(data)
	}
	if f, err := os.Open(fmt.Sprintf("/proc/%d/mem", r.PID)); err == nil {
		r.MemOpened = true
		f.Close()
	}
	_, err := os.ReadFile("/proc/self/environ")
	r.SelfReadable = err == nil
	_ = json.NewEncoder(os.Stdout).Encode(r)
	os.Exit(0)
}

func startAgent(t *testing.T, token string) (string, int) {
	t.Helper()
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := l.Addr().String()
	l.Close()
	var logs bytes.Buffer
	cmd := exec.Command(self)
	cmd.Env = []string{runAgentEnv + "=1", "AGENT_TOKEN=" + token, "AGENT_ADDR=" + addr, "AGENT_WORKDIR=" + t.TempDir(), "PATH=" + os.Getenv("PATH")}
	cmd.Stdout, cmd.Stderr = &logs, &logs
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
		if t.Failed() {
			t.Logf("agent 的输出：%s", logs.String())
		}
	})
	base := "http://" + addr
	for deadline := time.Now().Add(15 * time.Second); ; {
		if res, err := http.Get(base + "/healthz"); err == nil {
			res.Body.Close()
			if res.StatusCode == http.StatusOK {
				return base, cmd.Process.Pid
			}
		}
		if time.Now().After(deadline) {
			t.Fatal("agent 没有起来")
		}
		time.Sleep(50 * time.Millisecond)
	}
}

func TestCommandsCannotReadTheAgentTokenFromProc(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("root 有 CAP_SYS_PTRACE，能读任何进程的 /proc；这条要以非 root 跑（沙箱里命令是 10001）")
	}
	token := "proc-token-" + strings.Repeat("0123456789", 4)
	base, pid := startAgent(t, token)
	self, _ := os.Executable()
	body, _ := json.Marshal(map[string]any{"argv": []string{self, "proc-probe"}, "timeout_ms": 30000})
	req, _ := http.NewRequest(http.MethodPost, base+"/v1/exec", bytes.NewReader(body))
	req.Header.Set(agent.TokenHeader, token)
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	data, _ := io.ReadAll(res.Body)
	var out struct {
		ExitCode *int   `json:"exit_code"`
		Stdout   []byte `json:"stdout"`
		Stderr   []byte `json:"stderr"`
	}
	if res.StatusCode != http.StatusOK || json.Unmarshal(data, &out) != nil || out.ExitCode == nil || *out.ExitCode != 0 {
		t.Fatalf("exec：%d %s", res.StatusCode, data)
	}
	var r probeResult
	if err := json.Unmarshal(out.Stdout, &r); err != nil {
		t.Fatalf("探针的输出：%q %q", out.Stdout, out.Stderr)
	}
	if r.PID != pid {
		t.Fatalf("探针的父进程是 %d，agent 是 %d", r.PID, pid)
	}
	if strings.Contains(r.Environ, token) {
		t.Fatal("命令从 /proc/<agent>/environ 读到了 agent 的令牌")
	}
	if strings.Contains(r.Cmdline, token) {
		t.Fatal("命令从 /proc/<agent>/cmdline 读到了 agent 的令牌")
	}
	if r.EnvironErr == "" {
		t.Fatalf("/proc/<agent>/environ 不该让命令读：%q", r.Environ)
	}
	if r.MemOpened {
		t.Fatal("命令能打开 /proc/<agent>/mem，内存里的令牌就读得到")
	}
	if !r.SelfReadable {
		t.Fatal("命令自己的 /proc/self 要照常可读（只挡 agent，不挡命令）")
	}
}
