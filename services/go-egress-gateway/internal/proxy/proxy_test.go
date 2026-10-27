package proxy

import (
	"bytes"
	"crypto/tls"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providers/relational"
)

func b64(s string) string { return base64.StdEncoding.EncodeToString([]byte(s)) }

// pingPong 确认隧道已经接通（经回显服务往返一次）。
func pingPong(t *testing.T, conn net.Conn) {
	t.Helper()
	_ = conn.SetDeadline(time.Now().Add(3 * time.Second))
	if _, err := conn.Write([]byte("ping")); err != nil {
		t.Fatal(err)
	}
	got := make([]byte, 4)
	if _, err := io.ReadFull(conn, got); err != nil || string(got) != "ping" {
		t.Fatalf("隧道没有接通：%q %v", got, err)
	}
	_ = conn.SetDeadline(time.Time{})
}

// closedSoon 要求连接在 wait 之内被对方关掉（读到 EOF 或被重置，不是自己的超时）。
func closedSoon(t *testing.T, conn net.Conn, wait time.Duration) {
	t.Helper()
	_ = conn.SetReadDeadline(time.Now().Add(wait))
	_, err := conn.Read(make([]byte, 1))
	var ne net.Error
	if err == nil || (errors.As(err, &ne) && ne.Timeout()) {
		t.Fatalf("连接在 %s 内没有被断开：%v", wait, err)
	}
}

func auditByHost(rows []relational.Row, host string) relational.Row {
	for _, r := range rows {
		if r["host"] == host {
			return r
		}
	}
	return nil
}

// 验收 1：没有授权时连外网失败（包镜像不经网关，NO_PROXY，见 Worker 的 test_sandbox_egress.py）。
func TestWithoutGrantEverythingIsRefused(t *testing.T) {
	h := newHarness(t)
	status, body := h.get(h.client(h.token("egress", nil), true), "http://api.example.com/")
	if status != http.StatusForbidden || !strings.Contains(body, "no_grant") {
		t.Fatalf("没有授权：%d %s", status, body)
	}
	if h.dialer.dialedTo("93.184.216.34") {
		t.Fatal("没有授权也拨了上游")
	}
	conn, line := h.tunnel(h.token("egress", nil), "api.example.com:443")
	defer conn.Close()
	if !strings.Contains(line, "403") {
		t.Fatalf("CONNECT 没有授权：%s", line)
	}
}

func TestCredentialIsRequiredAndPurposeBound(t *testing.T) {
	h := newHarness(t)
	h.grant(grantSpec{id: "sbg-0000000000000001", hosts: []string{"api.example.com"}})
	for name, tok := range map[string]string{"none": "", "mirror": h.token("mirror", nil), "preview": h.token("preview", map[string]int64{"port": 80}), "junk": "ar1.x.y"} {
		status, _ := h.get(h.client(tok, true), "http://api.example.com/")
		if status != http.StatusProxyAuthRequired {
			t.Errorf("凭据 %s：%d，要 407", name, status)
		}
	}
}

// 验收 2：批准 api.example.com 15 分钟后可以访问它、访问别的主机被拒；到期后它也被拒。
func TestGrantAllowsOnlyItsHostsPortsAndTime(t *testing.T) {
	h := newHarness(t)
	h.grant(grantSpec{id: "sbg-0000000000000001", hosts: []string{"api.example.com"}, minutes: 15})
	c := h.client(h.token("egress", nil), true)
	if status, body := h.get(c, "http://api.example.com/"); status != 200 || body != "hello from api.example.com" {
		t.Fatalf("HTTP：%d %s", status, body)
	}
	if status, body := h.get(c, "https://api.example.com/"); status != 200 || !strings.HasPrefix(body, "hello from") {
		t.Fatalf("HTTPS：%d %s", status, body)
	}
	if status, body := h.get(c, "http://other.example.com/"); status != http.StatusForbidden || !strings.Contains(body, "host_not_granted") {
		t.Fatalf("别的主机：%d %s", status, body)
	}
	conn, line := h.tunnel(h.token("egress", nil), "api.example.com:7000")
	conn.Close()
	if !strings.Contains(line, "403") {
		t.Fatalf("没授权的端口：%s", line)
	}
	h.clock.Advance(16 * time.Minute)
	time.Sleep(250 * time.Millisecond) // 缓存过期
	if status, body := h.get(c, "http://api.example.com/"); status != http.StatusForbidden || !strings.Contains(body, "no_grant") {
		t.Fatalf("到期之后：%d %s", status, body)
	}
}

// 验收 3：SSRF 套件。
func TestSSRFSuite(t *testing.T) {
	h := newHarness(t)
	h.grant(grantSpec{id: "sbg-0000000000000001", hosts: []string{"*.example.com", "kubernetes.default.svc"}})
	c := h.client(h.token("egress", nil), true)
	cases := map[string]string{
		"http://169.254.169.254/latest/meta-data/": "ip_literal",
		"http://2130706433/":                       "ip_literal",
		"http://0x7f.1/":                           "ip_literal",
		"http://0177.0.0.1/":                       "ip_literal",
		"http://127.1/":                            "ip_literal",
		"http://[::1]/":                            "ip_literal",
		"http://[::ffff:169.254.169.254]/":         "ip_literal",
		"http://kubernetes.default.svc/":           "internal_name",
		"http://localhost/":                        "internal_name",
		"http://rebind.example.com/":               "blocked_address:private",
		"http://meta.example.com/":                 "blocked_address:cloud_metadata",
		"http://nat.example.com/":                  "blocked_address:private",
		"http://v6.example.com/":                   "blocked_address:unique_local",
	}
	for target, reason := range cases {
		status, body := h.get(c, target)
		if status != http.StatusForbidden || !strings.Contains(body, reason) {
			t.Errorf("%s：%d %s，要 403 %s", target, status, body, reason)
		}
	}
	for _, prefix := range []string{"10.", "169.254.", "127.", "[::", "[fd00"} {
		if h.dialer.dialedTo(prefix) {
			t.Errorf("拨了内网地址 %s…", prefix)
		}
	}
	// 重定向到内网：网关不跟随，客户端跟过去的那一次被拒。
	follow := h.client(h.token("egress", nil), false)
	status, body := h.get(follow, "http://api.example.com/redirect")
	if status != http.StatusForbidden || !strings.Contains(body, "ip_literal") {
		t.Fatalf("重定向到元数据地址：%d %s", status, body)
	}
	// CONNECT 写成地址也一样。
	conn, line := h.tunnel(h.token("egress", nil), "169.254.169.254:443")
	conn.Close()
	if !strings.Contains(line, "403") {
		t.Fatalf("CONNECT 地址：%s", line)
	}
}

// 验收 4：上行超过上限后连接被断开，审计记录里有原因。
func TestUploadLimitCutsTheConnection(t *testing.T) {
	h := newHarness(t)
	h.grant(grantSpec{id: "sbg-0000000000000001", hosts: []string{"api.example.com"}, ports: []int{80, 7000}, maxUp: 1000})
	conn, line := h.tunnel(h.token("egress", nil), "api.example.com:7000")
	defer conn.Close()
	if !strings.Contains(line, "200") {
		t.Fatalf("CONNECT：%s", line)
	}
	if _, err := conn.Write(bytes.Repeat([]byte("a"), 600)); err != nil {
		t.Fatal(err)
	}
	echo := make([]byte, 600)
	if _, err := io.ReadFull(conn, echo); err != nil {
		t.Fatalf("上限以内的数据应当通过：%v", err)
	}
	_, _ = conn.Write(bytes.Repeat([]byte("b"), 600))
	_ = conn.SetReadDeadline(time.Now().Add(3 * time.Second))
	if n, err := conn.Read(make([]byte, 600)); err == nil {
		t.Fatalf("超过上限后还收到了 %d 字节", n)
	}
	waitFor(t, "审计", func() bool {
		for _, r := range h.audits() {
			if r["reason"] == "up_bytes_exceeded" && r["result"] == "closed" {
				return true
			}
		}
		return false
	})
	// 同一个授权的下一次连接直接被拒。
	status, body := h.get(h.client(h.token("egress", nil), true), "http://api.example.com/")
	if status != http.StatusForbidden || !strings.Contains(body, "up_bytes_exceeded") {
		t.Fatalf("超过上限之后：%d %s", status, body)
	}
}

// 验收 5：一次任务访问过的主机、字节数与时长都在审计里（业务库与 trace 各一份；这里查业务库）。
func TestAuditRecordsHostsBytesAndDuration(t *testing.T) {
	h := newHarness(t)
	h.grant(grantSpec{id: "sbg-0000000000000001", hosts: []string{"api.example.com"}})
	c := h.client(h.token("egress", nil), true)
	h.get(c, "http://api.example.com/big")
	h.get(c, "http://other.example.com/")
	waitFor(t, "两条审计", func() bool { return len(h.audits()) == 2 })
	rows := h.audits()
	ok, denied := auditByHost(rows, "api.example.com"), auditByHost(rows, "other.example.com")
	if ok == nil || denied == nil {
		t.Fatalf("审计：%v", rows)
	}
	if ok["host"] != "api.example.com" || ok["result"] != "allowed" || ok["task_id"] != taskID || ok["grant_id"] != "sbg-0000000000000001" ||
		ok["address"] != "93.184.216.34" || ok["port"].(int64) != 80 || ok["down_bytes"].(int64) < 64*1024 || ok["direction"] != "egress" {
		t.Fatalf("放行的审计：%v", ok)
	}
	if denied["host"] != "other.example.com" || denied["result"] != "denied" || denied["reason"] != "host_not_granted" {
		t.Fatalf("拒绝的审计：%v", denied)
	}
	// 用量写回授权与当天的 sandbox_usage。
	if failed := h.store.Flush(t.Context()); failed != 0 {
		t.Fatalf("写回失败 %d", failed)
	}
	g := h.query("SELECT down_bytes FROM sandbox_grants WHERE grant_id = 'sbg-0000000000000001'")
	u := h.query("SELECT egress_bytes FROM sandbox_usage")
	if g[0]["down_bytes"].(int64) < 64*1024 || len(u) != 1 || u[0]["egress_bytes"].(int64) < 64*1024 {
		t.Fatalf("用量：%v %v", g, u)
	}
}

// 授权属于 Task：收回（管理接口或业务库里改状态）后开着的隧道断开，新的连接被拒。
func TestRevocationClosesOpenTunnels(t *testing.T) {
	h := newHarness(t)
	h.grant(grantSpec{id: "sbg-0000000000000001", hosts: []string{"api.example.com"}, ports: []int{7000}})
	conn, line := h.tunnel(h.token("egress", nil), "api.example.com:7000")
	defer conn.Close()
	if !strings.Contains(line, "200") {
		t.Fatalf("CONNECT：%s", line)
	}
	pingPong(t, conn)
	req, _ := http.NewRequest(http.MethodPost, h.admin.URL+"/admin/revoke", strings.NewReader(`{"tenant_id":"`+tenant+`","task_id":"`+taskID+`"}`))
	req.Header.Set("Authorization", "Bearer "+adminTk)
	resp, err := http.DefaultClient.Do(req)
	if err != nil || resp.StatusCode != 200 {
		t.Fatalf("收回：%v %v", resp, err)
	}
	var out map[string]int64
	_ = json.NewDecoder(resp.Body).Decode(&out)
	if out["revoked"] != 1 || out["closed"] != 1 {
		t.Fatalf("收回的结果：%v", out)
	}
	closedSoon(t, conn, 500*time.Millisecond)
	waitFor(t, "收回的审计", func() bool {
		for _, r := range h.audits() {
			if r["reason"] == "grant_revoked" {
				return true
			}
		}
		return false
	})
	conn2, line2 := h.tunnel(h.token("egress", nil), "api.example.com:7000")
	conn2.Close()
	if !strings.Contains(line2, "403") {
		t.Fatalf("收回之后的新连接：%s", line2)
	}
	// 管理接口要令牌。
	bad, _ := http.Post(h.admin.URL+"/admin/revoke", "application/json", strings.NewReader(`{"tenant_id":"t","task_id":"x"}`))
	if bad.StatusCode != http.StatusUnauthorized {
		t.Fatalf("没有令牌的收回：%d", bad.StatusCode)
	}
}

// Task 结束时 task-service 只改业务库（FinishTask），网关在缓存过期后自己发现并断开。
func TestTaskEndInStoreClosesTunnelWithinTTL(t *testing.T) {
	h := newHarness(t)
	h.grant(grantSpec{id: "sbg-0000000000000001", hosts: []string{"api.example.com"}, ports: []int{7000}})
	conn, line := h.tunnel(h.token("egress", nil), "api.example.com:7000")
	defer conn.Close()
	if !strings.Contains(line, "200") {
		t.Fatalf("CONNECT：%s", line)
	}
	pingPong(t, conn)
	h.exec("UPDATE sandbox_grants SET state = 'revoked' WHERE task_id = $1", taskID)
	closedSoon(t, conn, 2*time.Second) // 缓存 200 毫秒，核对间隔 100 毫秒
}

func TestDestroyedEnvironmentIsRefused(t *testing.T) {
	h := newHarness(t)
	h.grant(grantSpec{id: "sbg-0000000000000001", hosts: []string{"api.example.com"}})
	h.env(envRef, taskID, "destroyed")
	status, body := h.get(h.client(h.token("egress", nil), true), "http://api.example.com/")
	if status != http.StatusForbidden || !strings.Contains(body, "env_not_active") {
		t.Fatalf("环境销毁之后：%d %s", status, body)
	}
}

func TestConnectionAndRateLimits(t *testing.T) {
	h := newHarness(t)
	h.grant(grantSpec{id: "sbg-0000000000000001", hosts: []string{"api.example.com"}, ports: []int{7000}, maxConns: 1})
	first, line := h.tunnel(h.token("egress", nil), "api.example.com:7000")
	defer first.Close()
	if !strings.Contains(line, "200") {
		t.Fatalf("第一条：%s", line)
	}
	second, line2 := h.tunnel(h.token("egress", nil), "api.example.com:7000")
	second.Close()
	if !strings.Contains(line2, "429") {
		t.Fatalf("并发超过上限：%s", line2)
	}

	h2 := newHarness(t)
	h2.grant(grantSpec{id: "sbg-0000000000000002", hosts: []string{"api.example.com"}, rps: 2})
	c := h2.client(h2.token("egress", nil), true)
	codes := []int{}
	for range 4 {
		status, _ := h2.get(c, "http://api.example.com/")
		codes = append(codes, status)
	}
	if codes[0] != 200 || codes[1] != 200 || codes[3] != http.StatusTooManyRequests {
		t.Fatalf("每秒 2 个：%v", codes)
	}
}

// HTTPS 看 SNI：CONNECT 的是授权的主机，ClientHello 里却是别的名字，断开。
func TestSNIMustBeGranted(t *testing.T) {
	h := newHarness(t)
	h.grant(grantSpec{id: "sbg-0000000000000001", hosts: []string{"api.example.com"}})
	for _, sni := range []string{"evil.example.org", ""} {
		conn, line := h.tunnel(h.token("egress", nil), "api.example.com:443")
		if !strings.Contains(line, "200") {
			t.Fatalf("CONNECT：%s", line)
		}
		cfg := &tls.Config{InsecureSkipVerify: true, ServerName: sni}
		if sni == "" {
			cfg.ServerName = "" // 不发 SNI
		}
		tc := tls.Client(conn, cfg)
		_ = tc.SetDeadline(time.Now().Add(3 * time.Second))
		if err := tc.Handshake(); err == nil {
			t.Errorf("SNI %q 也握手成功了", sni)
		}
		conn.Close()
	}
	waitFor(t, "SNI 的审计", func() bool {
		reasons := map[any]bool{}
		for _, r := range h.audits() {
			reasons[r["reason"]] = true
		}
		return reasons["sni_not_granted"] && reasons["sni_missing"]
	})
}

func TestDailyEgressQuota(t *testing.T) {
	h := newHarness(t)
	h.grant(grantSpec{id: "sbg-0000000000000001", hosts: []string{"api.example.com"}})
	h.exec("INSERT INTO sandbox_usage (day, egress_bytes) VALUES ($1, 5000)", h.clock.Now().UTC().Format("2006-01-02"))
	status, body := h.get(h.client(h.token("egress", map[string]int64{"egress_bytes": 5000}), true), "http://api.example.com/")
	if status != http.StatusForbidden || !strings.Contains(body, "daily_egress_exceeded") {
		t.Fatalf("超了每天的出网字节：%d %s", status, body)
	}
	if status, _ := h.get(h.client(h.token("egress", map[string]int64{"egress_bytes": 50000}), true), "http://api.example.com/"); status != 200 {
		t.Fatalf("没超：%d", status)
	}
}

func TestParseSNIFromRealClientHello(t *testing.T) {
	client, server := net.Pipe()
	go func() {
		_ = tls.Client(client, &tls.Config{ServerName: "api.example.com", InsecureSkipVerify: true}).Handshake()
	}()
	buf := make([]byte, 16*1024)
	_ = server.SetReadDeadline(time.Now().Add(3 * time.Second))
	n, err := io.ReadAtLeast(server, buf, 5)
	if err != nil {
		t.Fatal(err)
	}
	want := 5 + (int(buf[3])<<8 | int(buf[4]))
	for n < want {
		m, err := server.Read(buf[n:])
		if err != nil {
			t.Fatal(err)
		}
		n += m
	}
	server.Close()
	if sni, ok := ParseSNI(buf[:want]); !ok || sni != "api.example.com" {
		t.Fatalf("ParseSNI = %q %v", sni, ok)
	}
	if _, ok := ParseSNI([]byte("GET / HTTP/1.1\r\n")); ok {
		t.Fatal("HTTP 不是 ClientHello")
	}
}
