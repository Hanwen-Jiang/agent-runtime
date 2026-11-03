package proxy

// 网络密钥（ENV-02；人工验收 Q28、Q31、Q32；docs/plan/env-02-implementation-2026-11-03.md 第 4 节）。
//
// 沙箱 CONNECT 到配了网络密钥的主机的 443 时，网关不原样转发，而是：
//
//   - 用网关 CA 现签这台主机的证书终结客户端的 TLS（SNI 必须就是 CONNECT 的主机；ALPN 只给 http/1.1）；
//   - 逐个读请求：Host 要与 CONNECT 的主机一致（否则 421），去掉沙箱自己写的同名头、逐跳的头，加上配置的头
//     （默认 Authorization: Bearer <值>，别的名字是值的原文）；值每个请求从 SecretStore 现读，读不到就 502、不发往上游；
//   - 以校验上游证书的 TLS（ServerName 是这台主机）连上游，把请求发过去、应答原样带回；
//   - 计量、授权到期与收回、审计与普通的隧道一样；值不进日志、审计与错误信息，也从不交给沙箱。

import (
	"bufio"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"errors"
	"io"
	"math/big"
	"net"
	"net/http"
	"net/netip"
	"strings"
	"sync/atomic"
	"time"

	"github.com/agentruntime/agent-runtime/services/go-egress-gateway/internal/grants"
	"github.com/agentruntime/agent-runtime/services/go-egress-gateway/internal/netguard"
)

// ParseCA 读网关 CA 的证书与私钥（PEM）。
func ParseCA(certPEM, keyPEM []byte) (*tls.Certificate, error) {
	cert, err := tls.X509KeyPair(certPEM, keyPEM)
	if err != nil {
		return nil, errors.New("网关 CA 的证书或私钥格式不对")
	}
	leaf, err := x509.ParseCertificate(cert.Certificate[0])
	if err != nil || !leaf.IsCA {
		return nil, errors.New("网关 CA 的证书不是 CA")
	}
	cert.Leaf = leaf
	return &cert, nil
}

// leafFor 是网关 CA 给这台主机签的证书（进程内按主机缓存，快到期时重签）。
func (g *Gateway) leafFor(host string) (*tls.Certificate, error) {
	now := g.cfg.Now()
	g.leafMu.Lock()
	defer g.leafMu.Unlock()
	if c, ok := g.leaves[host]; ok && c.Leaf != nil && now.Before(c.Leaf.NotAfter.Add(-time.Hour)) {
		return c, nil
	}
	ca := g.cfg.CA
	if ca == nil || ca.Leaf == nil {
		return nil, errors.New("网关 CA 不能签发证书")
	}
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, err
	}
	serial, _ := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 127))
	tmpl := &x509.Certificate{
		SerialNumber: serial, Subject: pkix.Name{CommonName: host}, DNSNames: []string{host},
		NotBefore: now.Add(-time.Hour), NotAfter: now.Add(24 * time.Hour),
		KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, ca.Leaf, &key.PublicKey, ca.PrivateKey)
	if err != nil {
		return nil, err
	}
	leaf, _ := x509.ParseCertificate(der)
	c := &tls.Certificate{Certificate: [][]byte{der, ca.Certificate[0]}, PrivateKey: key, Leaf: leaf}
	g.leaves[host] = c
	return c, nil
}

// prefixConn：先读 CONNECT 时缓冲里剩下的字节，再读连接本身。
type prefixConn struct {
	net.Conn
	r *bufio.Reader
}

func (c *prefixConn) Read(p []byte) (int, error) { return c.r.Read(p) }

// meteredConn 计上游方向的字节（含 TLS 的开销），超了授权的上限就断开。
type meteredConn struct {
	net.Conn
	g        *Gateway
	adm      *admission
	lc       *liveConn
	up, down atomic.Int64
}

func (c *meteredConn) Read(p []byte) (int, error) {
	n, err := c.Conn.Read(p)
	if n > 0 {
		c.down.Add(int64(n))
		c.g.downTotal.Add(int64(n))
		c.g.cfg.Store.AddUsage(c.adm.claims.TenantID, int64(n))
		if why := c.adm.meter.Add(c.adm.grant, 0, int64(n)); why != "" {
			c.lc.stop(why)
			return n, io.ErrUnexpectedEOF
		}
	}
	return n, err
}

func (c *meteredConn) Write(p []byte) (int, error) {
	n, err := c.Conn.Write(p)
	if n > 0 {
		c.up.Add(int64(n))
		c.g.upTotal.Add(int64(n))
		c.g.cfg.Store.AddUsage(c.adm.claims.TenantID, int64(n))
		if why := c.adm.meter.Add(c.adm.grant, int64(n), 0); why != "" {
			c.lc.stop(why)
			return n, io.ErrUnexpectedEOF
		}
	}
	return n, err
}

func (g *Gateway) handleIntercept(w http.ResponseWriter, r *http.Request, rec grants.Record, adm *admission, binding *grants.SecretHeader) {
	hj, ok := w.(http.Hijacker)
	if !ok {
		http.Error(w, "不支持 CONNECT", http.StatusInternalServerError)
		return
	}
	client, brw, err := hj.Hijack()
	if err != nil {
		return
	}
	if _, err := io.WriteString(client, "HTTP/1.1 200 Connection established\r\n\r\n"); err != nil {
		_ = client.Close()
		return
	}
	g.allowed.Add(1)
	ctx := r.Context()
	var upstream net.Conn
	lc := &liveConn{close: func() {
		_ = client.Close()
		if upstream != nil {
			_ = upstream.Close()
		}
	}}
	key := liveKey{adm.claims.TenantID, adm.sess.TaskID}
	g.track(key, lc)
	defer g.untrack(key, lc)
	g.active.Add(1)
	defer g.active.Add(-1)
	done := make(chan struct{})
	defer close(done)
	go func() {
		ticker := time.NewTicker(g.cfg.CheckInterval)
		defer ticker.Stop()
		for {
			select {
			case <-done:
				return
			case <-ticker.C:
				if why := g.stillGranted(ctx, adm); why != "" {
					lc.stop(why)
					return
				}
			}
		}
	}()
	why := g.intercept(ctx, adm, binding, &prefixConn{Conn: client, r: brw.Reader}, lc, &upstream)
	lc.stop("")
	if m, ok := upstream.(*meteredConn); ok {
		rec.UpBytes, rec.DownBytes = m.up.Load(), m.down.Load()
	}
	rec.Result, rec.Reason = "allowed", "secret_header"
	if closed := lc.why(); closed != "" {
		why = closed
	}
	if why != "" {
		rec.Result, rec.Reason = "closed", why
		if strings.HasPrefix(why, "sni_") || why == "host_mismatch" {
			rec.Result = "denied"
			g.denied.Add(1)
		} else {
			g.closedN.Add(1)
		}
		g.count(why)
	}
	g.finish(rec)
}

// intercept 终结客户端的 TLS 并逐个转发请求；返回结束的原因（正常结束为空）。
func (g *Gateway) intercept(ctx context.Context, adm *admission, binding *grants.SecretHeader, client net.Conn, lc *liveConn, upstream *net.Conn) string {
	tlsClient := tls.Server(client, &tls.Config{
		MinVersion: tls.VersionTLS12,
		NextProtos: []string{"http/1.1"},
		GetCertificate: func(hello *tls.ClientHelloInfo) (*tls.Certificate, error) {
			sni, reason := netguard.NormalizeHost(hello.ServerName)
			if reason != "" || sni != adm.host {
				return nil, errors.New("sni_mismatch")
			}
			return g.leafFor(adm.host)
		},
	})
	_ = tlsClient.SetDeadline(time.Now().Add(15 * time.Second))
	if err := tlsClient.HandshakeContext(ctx); err != nil {
		return "sni_not_granted"
	}
	_ = tlsClient.SetDeadline(time.Time{})
	raw, err := g.cfg.Dialer.DialContext(ctx, "tcp", netip.AddrPortFrom(adm.addr, 443).String())
	if err != nil {
		writeStatus(tlsClient, http.StatusBadGateway, "connect_failed")
		return "connect_failed"
	}
	metered := &meteredConn{Conn: raw, g: g, adm: adm, lc: lc}
	*upstream = metered
	up := tls.Client(metered, &tls.Config{ServerName: adm.host, RootCAs: g.cfg.UpstreamRoots, MinVersion: tls.VersionTLS12, NextProtos: []string{"http/1.1"}})
	if err := up.HandshakeContext(ctx); err != nil {
		writeStatus(tlsClient, http.StatusBadGateway, "upstream_tls_failed")
		return "upstream_tls_failed"
	}
	header := http.CanonicalHeaderKey(binding.Header)
	if header == "" {
		header = "Authorization"
	}
	fromClient, fromUpstream := bufio.NewReader(tlsClient), bufio.NewReader(up)
	for {
		req, err := http.ReadRequest(fromClient)
		if err != nil {
			return "" // 客户端关了连接
		}
		host, _, splitErr := net.SplitHostPort(req.Host)
		if splitErr != nil {
			host = req.Host
		}
		if h, reason := netguard.NormalizeHost(host); reason != "" || h != adm.host {
			writeStatus(tlsClient, http.StatusMisdirectedRequest, "host_mismatch")
			return "host_mismatch"
		}
		value, err := g.cfg.Secrets(ctx, adm.claims.TenantID, adm.sess.Env.SecretName(binding))
		if err != nil || len(value) == 0 {
			// 确认不了密钥就不发（人工验收 Q85）：不带空值或旧值继续。
			writeStatus(tlsClient, http.StatusBadGateway, "secret_unavailable")
			return "secret_unavailable"
		}
		for _, h := range hopHeaders {
			req.Header.Del(h)
		}
		req.Header.Del(header)
		if header == "Authorization" {
			req.Header.Set(header, "Bearer "+string(value))
		} else {
			req.Header.Set(header, string(value))
		}
		werr := req.Write(up)
		req.Header.Del(header)
		if werr != nil {
			return "upstream_failed"
		}
		resp, err := http.ReadResponse(fromUpstream, req)
		if err != nil {
			return "upstream_failed"
		}
		err = resp.Write(tlsClient)
		_ = resp.Body.Close()
		if err != nil || req.Close || resp.Close {
			return ""
		}
	}
}

func writeStatus(w io.Writer, status int, reason string) {
	body := "出网网关：" + reason
	resp := &http.Response{
		StatusCode: status, ProtoMajor: 1, ProtoMinor: 1, Header: http.Header{"Content-Type": {"text/plain; charset=utf-8"}},
		Body: io.NopCloser(strings.NewReader(body)), ContentLength: int64(len(body)), Close: true,
	}
	_ = resp.Write(w)
}
