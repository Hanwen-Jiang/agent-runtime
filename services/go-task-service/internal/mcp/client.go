package mcp

import (
	"bufio"
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/netip"
	"sort"
	"strings"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/netguard"
)

// ProtocolVersion 是客户端提出的 MCP 版本（Streamable HTTP）。
const ProtocolVersion = "2025-06-18"

// DiscoverTimeout 是建连接与刷新时连一次远端的上限（契约：至多 15 秒）。
const DiscoverTimeout = 15 * time.Second

const maxResponseBytes = 8 << 20

// RemoteTool 是远端 tools/list 给出的一个工具（数据，不是指令）。
type RemoteTool struct {
	Name        string          `json:"name"`
	Description string          `json:"description"`
	InputSchema json.RawMessage `json:"inputSchema"`
	Annotations *struct {
		ReadOnlyHint    *bool `json:"readOnlyHint"`
		DestructiveHint *bool `json:"destructiveHint"`
		IdempotentHint  *bool `json:"idempotentHint"`
		OpenWorldHint   *bool `json:"openWorldHint"`
	} `json:"annotations"`
}

// Discovery 是连一次远端的结果。
type Discovery struct {
	ServerName, ServerVersion string
	Tools                     []RemoteTool
}

// DiscoverError 带着连接的 error_code（mcp_unreachable 等）；Error() 只有原因，不带请求头的值。
type DiscoverError struct {
	Code   string
	Detail string
}

func (e *DiscoverError) Error() string { return e.Code + ": " + e.Detail }

func fail(code, detail string) error { return &DiscoverError{Code: code, Detail: detail} }

// Dialer 按 netguard 拨号：只连解析出、核对过是公网的地址（锁定这个地址，不再解析）。extra 是部署另配的拒绝网段。
type Dialer struct {
	Extra    []netip.Prefix
	Resolver *net.Resolver
}

// DialContext 给 http.Transport 用。
func (d Dialer) DialContext(ctx context.Context, network, address string) (net.Conn, error) {
	host, port, err := net.SplitHostPort(address)
	if err != nil {
		return nil, err
	}
	name, reason := netguard.NormalizeHost(host)
	if reason != "" {
		return nil, fail("mcp_host_not_public", reason)
	}
	resolver := d.Resolver
	if resolver == nil {
		resolver = net.DefaultResolver
	}
	addrs, err := resolver.LookupNetIP(ctx, "ip", name)
	if err != nil || len(addrs) == 0 {
		return nil, fail("mcp_unreachable", "域名解析不了")
	}
	for _, a := range addrs {
		if why := netguard.Check(a, d.Extra); why != "" {
			return nil, fail("mcp_host_not_public", "解析到非公网地址（"+why+"）")
		}
	}
	// IPv4 在前：集群里常常没有 IPv6 出口，先试 IPv6 会白白耗掉 15 秒的预算；每个地址至多 5 秒。
	sort.SliceStable(addrs, func(i, j int) bool { return addrs[i].Is4() && !addrs[j].Is4() })
	dialer := net.Dialer{Timeout: 5 * time.Second}
	var last error
	for _, a := range addrs {
		conn, err := dialer.DialContext(ctx, network, net.JoinHostPort(a.String(), port))
		if err == nil {
			return conn, nil
		}
		last = err
	}
	return nil, last
}

// Client 是建连接与刷新时用的 MCP 客户端（Streamable HTTP）：不跟重定向（重定向到别处时请求头不能带过去），按 netguard 拨号。
type Client struct {
	HTTP *http.Client
}

// NewClient 构造。
func NewClient(dialer Dialer) *Client {
	transport := &http.Transport{
		DialContext:         dialer.DialContext,
		TLSClientConfig:     &tls.Config{MinVersion: tls.VersionTLS12},
		TLSHandshakeTimeout: 10 * time.Second,
		ForceAttemptHTTP2:   true,
		MaxIdleConns:        16,
		IdleConnTimeout:     30 * time.Second,
		Proxy:               nil,
	}
	return &Client{HTTP: &http.Client{
		Transport:     transport,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}}
}

type rpcSession struct {
	c         *Client
	url       string
	header    string
	value     string
	sessionID string
	protocol  string
	nextID    int
}

type rpcMessage struct {
	ID     json.RawMessage `json:"id"`
	Result json.RawMessage `json:"result"`
	Error  *struct {
		Code    int    `json:"code"`
		Message string `json:"message"`
	} `json:"error"`
}

// classify 把传输层的错误归到 error_code；不带请求头的值。
func classify(err error) error {
	var de *DiscoverError
	if errors.As(err, &de) {
		return de
	}
	var cert *tls.CertificateVerificationError
	if errors.As(err, &cert) || strings.Contains(err.Error(), "tls:") || strings.Contains(err.Error(), "x509:") {
		return fail("mcp_tls_error", "TLS 握手或证书不对")
	}
	if errors.Is(err, context.DeadlineExceeded) {
		return fail("mcp_unreachable", "超时")
	}
	// 传输层的错误里只有地址与原因，没有请求头的值。
	return fail("mcp_unreachable", "连不上："+clip(err.Error(), 300))
}

func (s *rpcSession) post(ctx context.Context, body map[string]any, notify bool) (*rpcMessage, error) {
	data, _ := json.Marshal(body)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, s.url, bytes.NewReader(data))
	if err != nil {
		return nil, fail("mcp_protocol_error", "请求构造不了")
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json, text/event-stream")
	if s.protocol != "" {
		req.Header.Set("MCP-Protocol-Version", s.protocol)
	}
	if s.sessionID != "" {
		req.Header.Set("Mcp-Session-Id", s.sessionID)
	}
	if s.header != "" {
		req.Header.Set(s.header, s.value)
	}
	resp, err := s.c.HTTP.Do(req)
	if err != nil {
		return nil, classify(err)
	}
	defer resp.Body.Close()
	switch {
	case resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden:
		return nil, fail("mcp_auth_failed", resp.Status)
	case resp.StatusCode >= 300 && resp.StatusCode < 400:
		return nil, fail("mcp_protocol_error", "远端要求重定向（不跟）")
	case resp.StatusCode >= 400:
		return nil, fail("mcp_protocol_error", resp.Status)
	}
	if id := resp.Header.Get("Mcp-Session-Id"); id != "" {
		s.sessionID = id
	}
	if notify {
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, maxResponseBytes))
		return nil, nil
	}
	want, _ := json.Marshal(body["id"])
	ct := strings.ToLower(resp.Header.Get("Content-Type"))
	if strings.HasPrefix(ct, "text/event-stream") {
		return readSSE(resp.Body, want)
	}
	raw, err := io.ReadAll(io.LimitReader(resp.Body, maxResponseBytes))
	if err != nil {
		return nil, classify(err)
	}
	return pick(raw, want)
}

// pick 在一条 JSON-RPC 消息（或一批）里找 id 对得上的回应。
func pick(raw []byte, want []byte) (*rpcMessage, error) {
	raw = bytes.TrimSpace(raw)
	var batch []rpcMessage
	if len(raw) > 0 && raw[0] == '[' {
		if err := json.Unmarshal(raw, &batch); err != nil {
			return nil, fail("mcp_protocol_error", "回应不是 JSON-RPC")
		}
	} else {
		var one rpcMessage
		if err := json.Unmarshal(raw, &one); err != nil {
			return nil, fail("mcp_protocol_error", "回应不是 JSON-RPC")
		}
		batch = []rpcMessage{one}
	}
	for i := range batch {
		if bytes.Equal(bytes.TrimSpace(batch[i].ID), want) {
			return &batch[i], nil
		}
	}
	return nil, nil
}

func readSSE(body io.Reader, want []byte) (*rpcMessage, error) {
	scanner := bufio.NewScanner(io.LimitReader(body, maxResponseBytes))
	scanner.Buffer(make([]byte, 64<<10), maxResponseBytes)
	var data strings.Builder
	flush := func() (*rpcMessage, error) {
		if data.Len() == 0 {
			return nil, nil
		}
		text := data.String()
		data.Reset()
		return pick([]byte(text), want)
	}
	for scanner.Scan() {
		line := scanner.Text()
		if line == "" {
			if msg, err := flush(); msg != nil || err != nil {
				if err != nil {
					continue // 不是 JSON-RPC 的事件跳过
				}
				return msg, nil
			}
			continue
		}
		if v, ok := strings.CutPrefix(line, "data:"); ok {
			if data.Len() > 0 {
				data.WriteByte('\n')
			}
			data.WriteString(strings.TrimPrefix(v, " "))
		}
	}
	if msg, err := flush(); msg != nil && err == nil {
		return msg, nil
	}
	if err := scanner.Err(); err != nil {
		return nil, classify(err)
	}
	return nil, fail("mcp_protocol_error", "事件流结束了还没有回应")
}

func (s *rpcSession) call(ctx context.Context, method string, params map[string]any) (json.RawMessage, error) {
	s.nextID++
	msg, err := s.post(ctx, map[string]any{"jsonrpc": "2.0", "id": s.nextID, "method": method, "params": params}, false)
	if err != nil {
		return nil, err
	}
	if msg == nil {
		return nil, fail("mcp_protocol_error", method+" 没有回应")
	}
	if msg.Error != nil {
		return nil, fail("mcp_protocol_error", fmt.Sprintf("%s 报错 %d", method, msg.Error.Code))
	}
	return msg.Result, nil
}

// Discover 连一次远端：initialize、notifications/initialized、tools/list（翻页），然后结束会话。header 为空时不带网络密钥。
func (c *Client) Discover(ctx context.Context, url, header, value string) (Discovery, error) {
	ctx, cancel := context.WithTimeout(ctx, DiscoverTimeout)
	defer cancel()
	s := &rpcSession{c: c, url: url, header: header, value: value}
	raw, err := s.call(ctx, "initialize", map[string]any{
		"protocolVersion": ProtocolVersion, "capabilities": map[string]any{},
		"clientInfo": map[string]any{"name": "agentruntime-mcp", "version": "1"},
	})
	if err != nil {
		return Discovery{}, err
	}
	var init struct {
		ProtocolVersion string `json:"protocolVersion"`
		ServerInfo      struct {
			Name    string `json:"name"`
			Version string `json:"version"`
		} `json:"serverInfo"`
	}
	if err := json.Unmarshal(raw, &init); err != nil {
		return Discovery{}, fail("mcp_protocol_error", "initialize 的回应不对")
	}
	s.protocol = init.ProtocolVersion
	if s.protocol == "" {
		s.protocol = ProtocolVersion
	}
	if _, err := s.post(ctx, map[string]any{"jsonrpc": "2.0", "method": "notifications/initialized"}, true); err != nil {
		return Discovery{}, err
	}
	out := Discovery{ServerName: clip(init.ServerInfo.Name, 200), ServerVersion: clip(init.ServerInfo.Version, 100)}
	cursor := ""
	for page := 0; page < 50; page++ {
		params := map[string]any{}
		if cursor != "" {
			params["cursor"] = cursor
		}
		raw, err := s.call(ctx, "tools/list", params)
		if err != nil {
			return Discovery{}, err
		}
		var list struct {
			Tools      []RemoteTool `json:"tools"`
			NextCursor string       `json:"nextCursor"`
		}
		if err := json.Unmarshal(raw, &list); err != nil {
			return Discovery{}, fail("mcp_protocol_error", "tools/list 的回应不对")
		}
		out.Tools = append(out.Tools, list.Tools...)
		if cursor = list.NextCursor; cursor == "" {
			break
		}
	}
	if s.sessionID != "" {
		// 结束会话（尽力而为）。
		if req, err := http.NewRequestWithContext(ctx, http.MethodDelete, url, nil); err == nil {
			req.Header.Set("Mcp-Session-Id", s.sessionID)
			if header != "" {
				req.Header.Set(header, value)
			}
			if resp, err := c.HTTP.Do(req); err == nil {
				_ = resp.Body.Close()
			}
		}
	}
	return out, nil
}

func clip(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}
