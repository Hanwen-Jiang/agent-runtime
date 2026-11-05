// Package cfbridgetest 是测试里的 Cloudflare 沙箱桥（D-07）：与 deploy/cloudflare/sandbox-bridge 同一套 HTTP 接口，每个环境
// 起一个真的 sandbox-agent 进程（testkit.SandboxAgentBinary），转发规则也相同：核对桥的令牌与 X-Sandbox-Owner，agent 的令牌
// 只在桥里。用来跑 Cloudflare 适配器的一致性测试；microVM、出网白名单、空闲回收测不到，真实端点设 AR_LIVE_CLOUDFLARE=1。
// （SBX-01 从 Python 的 agentruntime_core.adapters.cloudflaretest 搬来。）
package cfbridgetest

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/http/httputil"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
)

var owner = regexp.MustCompile(`^(_platform|[a-z0-9][a-z0-9_-]{0,62})$`)

// flavor：Cloudflare 桥是 /v1/sandboxes 与 cfs- 开头的 ID；fc-manager（D-20，services/rs-fc-manager）同形，只是 /v1/envs 与 fc-。
type flavor struct {
	base    string
	prefix  string
	envPath *regexp.Regexp
	envID   *regexp.Regexp
}

var (
	bridgeFlavor = flavor{"/v1/sandboxes", "cfs-", regexp.MustCompile(`^/v1/sandboxes/([^/]+)(/agent(/.*))?$`), regexp.MustCompile(`^cfs-[0-9a-f]{16}$`)}
	fcFlavor     = flavor{"/v1/envs", "fc-", regexp.MustCompile(`^/v1/envs/([^/]+)(/agent(/.*))?$`), regexp.MustCompile(`^fc-[0-9a-f]{16}$`)}
)

type env struct {
	owner     string
	cmd       *exec.Cmd
	port      int
	token     string
	workdir   string
	createdAt string
}

// Bridge 是一个假桥。
type Bridge struct {
	URL   string
	Token string

	server *httptest.Server
	flavor flavor
	agent  string
	dir    string
	key    []byte

	mu      sync.Mutex
	envs    map[string]*env
	created []map[string]any
}

// New 起一个假桥（要 PATH 上的 go，编译 sandbox-agent）。用完 Close。
func New() (*Bridge, error) { return start(bridgeFlavor) }

// NewFCManager 起一个假的 fc-manager（接口同桥，路径是 /v1/envs，环境 ID 是 fc- 开头；没有 VM，每个环境一个本机的 agent 进程）。
func NewFCManager() (*Bridge, error) { return start(fcFlavor) }

func start(f flavor) (*Bridge, error) {
	agent, err := testkit.SandboxAgentBinary()
	if err != nil {
		return nil, err
	}
	dir, err := os.MkdirTemp("", "ar-cfbridge-")
	if err != nil {
		return nil, err
	}
	b := &Bridge{Token: randomHex(24), flavor: f, agent: agent, dir: dir, key: []byte(randomHex(32)), envs: map[string]*env{}}
	b.server = httptest.NewServer(http.HandlerFunc(b.handle))
	b.URL = b.server.URL
	return b, nil
}

// Created 是建环境时收到的参数（按顺序），每项多一个 owner。
func (b *Bridge) Created() []map[string]any {
	b.mu.Lock()
	defer b.mu.Unlock()
	return append([]map[string]any(nil), b.created...)
}

// Close 停掉所有环境与桥。
func (b *Bridge) Close() {
	b.mu.Lock()
	ids := make([]string, 0, len(b.envs))
	for id := range b.envs {
		ids = append(ids, id)
	}
	b.mu.Unlock()
	for _, id := range ids {
		b.stop(id)
	}
	b.server.Close()
	_ = os.RemoveAll(b.dir)
}

func (b *Bridge) handle(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path == "/healthz" {
		_, _ = io.WriteString(w, "ok")
		return
	}
	if subtle.ConstantTimeCompare([]byte(r.Header.Get("Authorization")), []byte("Bearer "+b.Token)) != 1 {
		problem(w, http.StatusUnauthorized, "unauthorized")
		return
	}
	who := r.Header.Get("X-Sandbox-Owner")
	if !owner.MatchString(who) {
		problem(w, http.StatusBadRequest, "invalid_owner")
		return
	}
	if r.URL.Path == b.flavor.base {
		switch r.Method {
		case http.MethodGet:
			if who != "_platform" {
				problem(w, http.StatusForbidden, "platform_only")
				return
			}
			b.mu.Lock()
			items := []map[string]string{}
			for id, e := range b.envs {
				items = append(items, map[string]string{"env_id": id, "owner": e.owner, "created_at": e.createdAt})
			}
			b.mu.Unlock()
			sort.Slice(items, func(i, j int) bool { return items[i]["env_id"] < items[j]["env_id"] })
			writeJSON(w, http.StatusOK, map[string]any{"environments": items})
		case http.MethodPost:
			spec := map[string]any{}
			_ = json.NewDecoder(r.Body).Decode(&spec)
			id := b.flavor.prefix + randomHex(8)
			if err := b.start(id, who); err != nil {
				problem(w, http.StatusServiceUnavailable, "start_failed")
				return
			}
			spec["owner"] = who
			b.mu.Lock()
			b.created = append(b.created, spec)
			b.mu.Unlock()
			writeJSON(w, http.StatusCreated, map[string]string{"env_id": id})
		default:
			problem(w, http.StatusMethodNotAllowed, "method_not_allowed")
		}
		return
	}
	m := b.flavor.envPath.FindStringSubmatch(r.URL.Path)
	if m == nil || !b.flavor.envID.MatchString(m[1]) {
		problem(w, http.StatusNotFound, "unknown_env")
		return
	}
	b.mu.Lock()
	e := b.envs[m[1]]
	b.mu.Unlock()
	if r.Method == http.MethodDelete && m[2] == "" {
		if e != nil && e.owner != who {
			problem(w, http.StatusNotFound, "unknown_env")
			return
		}
		b.stop(m[1])
		w.WriteHeader(http.StatusNoContent)
		return
	}
	if e == nil || e.owner != who || m[3] == "" {
		problem(w, http.StatusNotFound, "unknown_env")
		return
	}
	target, _ := url.Parse(fmt.Sprintf("http://127.0.0.1:%d", e.port))
	proxy := &httputil.ReverseProxy{
		Rewrite: func(pr *httputil.ProxyRequest) {
			pr.SetURL(target)
			pr.Out.URL.Path = m[3]
			pr.Out.URL.RawPath = ""
			if i := strings.Index(r.URL.EscapedPath(), "/agent/"); i >= 0 {
				pr.Out.URL.RawPath = r.URL.EscapedPath()[i+len("/agent"):]
			}
			pr.Out.Header.Del("Authorization")
			pr.Out.Header.Del("X-Sandbox-Owner")
			pr.Out.Header.Set("X-Sandbox-Token", e.token)
		},
		FlushInterval: -1,
	}
	proxy.ServeHTTP(w, r)
}

func (b *Bridge) start(id, who string) error {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return err
	}
	port := l.Addr().(*net.TCPAddr).Port
	_ = l.Close()
	mac := hmac.New(sha256.New, b.key)
	mac.Write([]byte(id))
	token := hex.EncodeToString(mac.Sum(nil))
	workdir := filepath.Join(b.dir, id)
	if err := os.MkdirAll(workdir, 0o700); err != nil {
		return err
	}
	cmd := exec.Command(b.agent)
	cmd.Env = append(os.Environ(), "AGENT_TOKEN="+token, "AGENT_WORKDIR="+workdir, fmt.Sprintf("AGENT_ADDR=127.0.0.1:%d", port))
	if err := cmd.Start(); err != nil {
		return err
	}
	deadline := time.Now().Add(30 * time.Second)
	for {
		resp, err := http.Get(fmt.Sprintf("http://127.0.0.1:%d/healthz", port))
		if err == nil {
			resp.Body.Close()
			break
		}
		if time.Now().After(deadline) {
			_ = cmd.Process.Kill()
			_ = cmd.Wait()
			return fmt.Errorf("sandbox-agent 没有起来")
		}
		time.Sleep(50 * time.Millisecond)
	}
	b.mu.Lock()
	b.envs[id] = &env{owner: who, cmd: cmd, port: port, token: token, workdir: workdir, createdAt: time.Now().UTC().Format(time.RFC3339)}
	b.mu.Unlock()
	return nil
}

func (b *Bridge) stop(id string) {
	b.mu.Lock()
	e := b.envs[id]
	delete(b.envs, id)
	b.mu.Unlock()
	if e == nil {
		return
	}
	_ = e.cmd.Process.Kill()
	_ = e.cmd.Wait()
	_ = os.RemoveAll(e.workdir)
}

func problem(w http.ResponseWriter, status int, code string) {
	writeJSON(w, status, map[string]string{"code": code, "message": code})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func randomHex(n int) string {
	buf := make([]byte, n)
	_, _ = rand.Read(buf)
	return hex.EncodeToString(buf)
}
