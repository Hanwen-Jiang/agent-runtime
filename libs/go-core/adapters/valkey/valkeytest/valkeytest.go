// Package valkeytest 在测试里拉起临时的 Valkey（CACHE-01）。
//
// 设了 AR_VALKEY_TEST_URL 时直接用它（例如 CI 的服务容器；要带口令）。否则用 PATH 上（或 AR_DOCKER_BIN 指定位置）的
// docker 起一个一次性的容器：不落盘（--save "" --appendonly no），端口只绑 127.0.0.1，带随机口令，测完删除。
// 镜像默认 valkey/valkey:9.1.2，可用 AR_VALKEY_IMAGE 覆盖。找不到 docker 时测试失败；设 AR_ALLOW_MISSING_TOOLS=1 改为跳过。
//
// 用法：测试包的 TestMain 调 Main，用例里用 Current(t) 拿到实例；每个用例用 Prefix(t) 取一个新的键前缀，用例之间不用清库。
package valkeytest

import (
	"bytes"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"net"
	"net/url"
	"os"
	"os/exec"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// DefaultImage 是测试用的镜像。
const DefaultImage = "valkey/valkey:9.1.2"

// Server 是一个临时的 Valkey。
type Server struct {
	// Addr 是 host:port。
	Addr string
	// Password 是 default 用户的口令。
	Password  string
	container string
	docker    string
}

var (
	current  *Server
	startErr error
	prefixes atomic.Int64
)

func randomHex(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

func run(name string, args ...string) (string, error) {
	cmd := exec.Command(name, args...)
	var out bytes.Buffer
	cmd.Stdout, cmd.Stderr = &out, &out
	err := cmd.Run()
	return strings.TrimSpace(out.String()), err
}

func start() (*Server, error) {
	if raw := os.Getenv("AR_VALKEY_TEST_URL"); raw != "" {
		u, err := url.Parse(raw)
		if err != nil || u.Host == "" {
			return nil, errors.New("AR_VALKEY_TEST_URL 不是 valkey://:口令@主机:端口 的形式")
		}
		pw, _ := u.User.Password()
		return &Server{Addr: u.Host, Password: pw}, nil
	}
	docker := os.Getenv("AR_DOCKER_BIN")
	if docker == "" {
		p, err := exec.LookPath("docker")
		if err != nil {
			return nil, fmt.Errorf("找不到 docker（Valkey 适配器的测试用它拉起容器），见 CLAUDE.md 的常用命令：%w", err)
		}
		docker = p
	}
	image := os.Getenv("AR_VALKEY_IMAGE")
	if image == "" {
		image = DefaultImage
	}
	s := &Server{Password: randomHex(16), container: "ar-valkey-test-" + randomHex(4), docker: docker}
	// 口令是本次测试随机生成的，只用于这个一次性的容器。
	if out, err := run(docker, "run", "-d", "--rm", "--name", s.container, "-p", "127.0.0.1::6379", image,
		"valkey-server", "--save", "", "--appendonly", "no", "--requirepass", s.Password); err != nil {
		return nil, fmt.Errorf("docker run %s 失败：%v\n%s", image, err, out)
	}
	out, err := run(docker, "port", s.container, "6379/tcp")
	if err != nil {
		s.stop()
		return nil, fmt.Errorf("docker port 失败：%v\n%s", err, out)
	}
	// 可能有 IPv4、IPv6 两行，取 127.0.0.1 那一行。
	for _, line := range strings.Split(out, "\n") {
		if strings.HasPrefix(strings.TrimSpace(line), "127.0.0.1:") {
			s.Addr = strings.TrimSpace(line)
		}
	}
	if s.Addr == "" {
		s.stop()
		return nil, fmt.Errorf("docker port 没给出 127.0.0.1 的端口：%s", out)
	}
	if err := s.waitReady(60 * time.Second); err != nil {
		logs, _ := run(docker, "logs", s.container)
		s.stop()
		return nil, fmt.Errorf("Valkey 没有就绪：%v\n%s", err, logs)
	}
	return s, nil
}

// waitReady 等到 AUTH 加 PING 返回 PONG（端口映射先通、服务后起，只连上 TCP 不算）。
func (s *Server) waitReady(limit time.Duration) error {
	deadline := time.Now().Add(limit)
	var last error
	for time.Now().Before(deadline) {
		if last = s.ping(); last == nil {
			return nil
		}
		time.Sleep(200 * time.Millisecond)
	}
	return last
}

func (s *Server) ping() error {
	conn, err := net.DialTimeout("tcp", s.Addr, 2*time.Second)
	if err != nil {
		return err
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(2 * time.Second))
	cmd := fmt.Sprintf("*2\r\n$4\r\nAUTH\r\n$%d\r\n%s\r\n*1\r\n$4\r\nPING\r\n", len(s.Password), s.Password)
	if _, err := conn.Write([]byte(cmd)); err != nil {
		return err
	}
	buf := make([]byte, 64)
	var got []byte
	for !bytes.Contains(got, []byte("PONG")) {
		n, err := conn.Read(buf)
		if err != nil {
			return err
		}
		got = append(got, buf[:n]...)
		if bytes.HasPrefix(got, []byte("-")) {
			return errors.New("AUTH 被拒")
		}
	}
	return nil
}

func (s *Server) stop() {
	if s.container != "" {
		_, _ = run(s.docker, "rm", "-f", s.container)
	}
}

// Main 在 TestMain 里调用：启动实例，跑完用例后删除容器。
func Main(m *testing.M) int {
	current, startErr = start()
	code := m.Run()
	if current != nil {
		current.stop()
	}
	return code
}

// Current 返回本测试进程的实例；实例没起来时让用例失败（或按 AR_ALLOW_MISSING_TOOLS 跳过）。
func Current(tb testing.TB) *Server {
	tb.Helper()
	if current == nil {
		if startErr == nil {
			startErr = errors.New("没有调用 valkeytest.Main")
		}
		if os.Getenv("AR_ALLOW_MISSING_TOOLS") == "1" {
			tb.Skip(startErr.Error())
		}
		tb.Fatal(startErr)
	}
	return current
}

// URL 返回带口令的连接地址。
func (s *Server) URL() string {
	return (&url.URL{Scheme: "valkey", User: url.UserPassword("default", s.Password), Host: s.Addr}).String()
}

// Prefix 返回一个本进程内唯一的键前缀，让每个用例从空的命名空间开始。
func Prefix(tb testing.TB) string {
	tb.Helper()
	return fmt.Sprintf("t%s-%d:", randomHex(3), prefixes.Add(1))
}
