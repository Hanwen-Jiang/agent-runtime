package proxy

import (
	"crypto/tls"
	"net/http"
	"net/netip"
	"strings"
	"testing"
	"time"
)

func publicGrant(t *testing.T) *harness {
	h := newHarness(t)
	h.grant(grantSpec{id: "sbg-public", ports: []int{80, 443, 25}, maxConns: 4})
	h.exec("UPDATE sandbox_grants SET scope = 'public' WHERE grant_id = 'sbg-public'")
	return h
}

func TestPublicGrantUsesTheSameSecurityBoundary(t *testing.T) {
	h := publicGrant(t)
	h.resolver.answers["loop.example.com"] = []netip.Addr{netip.MustParseAddr("127.0.0.1")}
	h.resolver.answers["cgnat.example.com"] = []netip.Addr{netip.MustParseAddr("100.64.0.1")}
	c := h.client(h.token("egress", nil), true)
	for _, host := range []string{"api.example.com", "other.example.com"} {
		if status, body := h.get(c, "http://"+host+"/"); status != 200 {
			t.Fatalf("%s: %d %s", host, status, body)
		}
	}
	for target, reason := range map[string]string{
		"http://169.254.169.254/": "public_ip_literal", "http://127.1/": "public_ip_literal",
		"http://rebind.example.com/": "public_private_address", "http://meta.example.com/": "public_private_address",
		"http://loop.example.com/": "public_private_address", "http://cgnat.example.com/": "public_private_address",
		"http://api.example.com:25/": "port_25",
	} {
		if status, body := h.get(c, target); status != http.StatusForbidden || !strings.Contains(body, reason) {
			t.Errorf("%s: %d %s", target, status, body)
		}
	}
	if status, body := h.get(h.client(h.token("egress", nil), false), "http://api.example.com/redirect"); status != 403 || !strings.Contains(body, "public_ip_literal") {
		t.Fatalf("redirect: %d %s", status, body)
	}
	for _, prefix := range []string{"10.", "127.", "100.64.", "169.254."} {
		if h.dialer.dialedTo(prefix) {
			t.Fatalf("private dial: %s", prefix)
		}
	}
	h.clock.Advance(16 * time.Minute)
	time.Sleep(250 * time.Millisecond)
	if status, _ := h.get(c, "http://api.example.com/"); status != 403 {
		t.Fatalf("expired: %d", status)
	}
}

func TestPublicConnectRejectsAnotherPublicSNI(t *testing.T) {
	h := publicGrant(t)
	conn, line := h.tunnel(h.token("egress", nil), "api.example.com:443")
	defer conn.Close()
	if !strings.Contains(line, "200") {
		t.Fatal(line)
	}
	client := tls.Client(conn, &tls.Config{InsecureSkipVerify: true, ServerName: "other.example.com"})
	_ = client.SetDeadline(time.Now().Add(3 * time.Second))
	if err := client.Handshake(); err == nil {
		t.Fatal("mismatched public SNI accepted")
	}
	waitFor(t, "SNI mismatch audit", func() bool {
		for _, r := range h.audits() {
			if r["reason"] == "sni_mismatch" {
				return true
			}
		}
		return false
	})
}

func TestUnknownScopeCannotGrantAccess(t *testing.T) {
	h := publicGrant(t)
	h.exec(`UPDATE sandbox_grants SET scope = 'unknown', hosts = '["api.example.com"]'`)
	if status, _ := h.get(h.client(h.token("egress", nil), true), "http://api.example.com/"); status != 403 {
		t.Fatalf("unknown scope: %d", status)
	}
}

func TestPublicGrantDoesNotExpandSecretHeaders(t *testing.T) {
	sh := newSecretHarness(t, defaultEnvironment())
	sh.exec("UPDATE sandbox_grants SET scope = 'public', hosts = '[]'")
	c := sh.client(sh.token("egress", nil), true)
	resp, err := c.Get("https://api.example.com/")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	headers, _ := sh.upstream.last()
	if headers.Get("Authorization") != "Bearer "+fakeToken {
		t.Fatal("configured secret missing")
	}
	resp, err = c.Get("https://other.example.com/")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	headers, _ = sh.upstream.last()
	if headers.Get("Authorization") != "" || headers.Get("X-Api-Key") != "" {
		t.Fatal("secret expanded to another host")
	}
	sh.assertNoTokenAnywhere(t)
}

func TestPublicGrantStillAppliesEnvironmentDeny(t *testing.T) {
	env := defaultEnvironment()
	env["network"] = map[string]any{"mode": "deny"}
	sh := newSecretHarness(t, env)
	sh.exec("UPDATE sandbox_grants SET scope = 'public', hosts = '[]'")
	status, body := sh.get(sh.client(sh.token("egress", nil), true), "http://api.example.com/")
	if status != 403 || !strings.Contains(body, "environment_network_denied") {
		t.Fatalf("%d %s", status, body)
	}
}

func TestPublicGrantRespectsRateLimitAndRevocation(t *testing.T) {
	h := publicGrant(t)
	h.exec("UPDATE sandbox_grants SET max_rps = 1")
	c := h.client(h.token("egress", nil), true)
	if status, _ := h.get(c, "http://api.example.com/"); status != 200 {
		t.Fatal(status)
	}
	if status, _ := h.get(c, "http://other.example.com/"); status != 429 {
		t.Fatalf("public rate limit %d", status)
	}
	h.exec("UPDATE sandbox_grants SET state = 'revoked'")
	h.store.Invalidate(tenant)
	if status, _ := h.get(c, "http://api.example.com/"); status != 403 {
		t.Fatalf("revoked public grant %d", status)
	}
}
