package valkey

// 最小的 RESP2 客户端（只用标准库，不引第三方驱动；同 K8s 的 REST 客户端与 SigV4 的做法）：
// 连接池、AUTH 与 SELECT、按 ctx 设读写期限、脚本走 EVALSHA 并在 NOSCRIPT 时回退 EVAL。

import (
	"bufio"
	"context"
	"crypto/sha1"
	"crypto/tls"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/secret"
)

// serverError 是 Valkey 回的错误（RESP 的 -ERR …）。文本只作 cause，不进 providererr 的 Error()。
type serverError string

func (e serverError) Error() string { return string(e) }

// prefix 是错误的第一个词，例如 NOSCRIPT、WRONGPASS。
func (e serverError) prefix() string {
	p, _, _ := strings.Cut(string(e), " ")
	return p
}

// classify 把底层错误映射到错误模型（providererr 的文本里不带服务端原文与地址）。
func classify(err error) error {
	if err == nil {
		return nil
	}
	var pe *providererr.Error
	if errors.As(err, &pe) {
		return pe
	}
	var se serverError
	if errors.As(err, &se) {
		switch se.prefix() {
		case "NOAUTH", "WRONGPASS", "NOPERM":
			return providererr.Wrap(providererr.Denied, "denied", "Valkey 拒绝了认证或权限", err)
		case "LOADING", "BUSY", "TRYAGAIN", "CLUSTERDOWN", "MASTERDOWN", "READONLY", "OOM", "MOVED", "ASK":
			return providererr.Wrap(providererr.Transient, "unavailable", "Valkey 暂时不可用", err)
		default:
			return providererr.Wrap(providererr.Invalid, "server_error", "Valkey 拒绝了命令", err)
		}
	}
	if errors.Is(err, context.DeadlineExceeded) || errors.Is(err, context.Canceled) {
		return providererr.Wrap(providererr.Transient, "timeout", "等待 Valkey 超时", err)
	}
	var ne net.Error
	if errors.As(err, &ne) && ne.Timeout() {
		return providererr.Wrap(providererr.Transient, "timeout", "等待 Valkey 超时", err)
	}
	return providererr.Wrap(providererr.Transient, "unavailable", "连不上 Valkey", err)
}

// endpoint 是解析后的连接地址。password 按密钥对待。
type endpoint struct {
	addr     string
	tls      bool
	host     string
	username string
	password secret.Value
	db       int
}

// parseURL 解析 valkey://、redis://（明文）与 valkeys://、rediss://（TLS）：[用户[:口令]@]主机[:端口][/库号]。
// 错误信息不带原文（里面可能有口令）。
func parseURL(raw secret.Value) (endpoint, error) {
	bad := func(why string) (endpoint, error) {
		return endpoint{}, providererr.New(providererr.Invalid, "invalid_argument", "Valkey 地址"+why+"；形如 valkey://:口令@主机:6379/0")
	}
	if raw.IsZero() {
		return bad("为空")
	}
	u, err := parseRawURL(string(raw.Reveal()))
	if err != nil {
		return bad("不是合法的 URL")
	}
	var ep endpoint
	switch u.scheme {
	case "valkey", "redis":
	case "valkeys", "rediss":
		ep.tls = true
	default:
		return bad("的协议只能是 valkey、redis、valkeys 或 rediss")
	}
	if u.host == "" {
		return bad("缺少主机")
	}
	ep.host = u.host
	port := u.port
	if port == "" {
		port = "6379"
	}
	if _, err := strconv.Atoi(port); err != nil {
		return bad("的端口不是数字")
	}
	ep.addr = net.JoinHostPort(u.host, port)
	ep.username = u.username
	if u.password != "" {
		ep.password = secret.New(u.password)
	}
	if db := strings.Trim(u.path, "/"); db != "" {
		n, err := strconv.Atoi(db)
		if err != nil || n < 0 {
			return bad("的库号不是非负整数")
		}
		ep.db = n
	}
	return ep, nil
}

type rawURL struct{ scheme, username, password, host, port, path string }

// parseRawURL 用 net/url 解析；单独成函数是为了让 *url.Error（文本里带原 URL）不外泄。
func parseRawURL(s string) (rawURL, error) {
	u, err := url.Parse(s)
	if err != nil {
		return rawURL{}, errors.New("parse")
	}
	r := rawURL{scheme: u.Scheme, host: u.Hostname(), port: u.Port(), path: u.Path}
	if u.User != nil {
		r.username = u.User.Username()
		r.password, _ = u.User.Password()
	}
	return r, nil
}

type conn struct {
	nc net.Conn
	r  *bufio.Reader
	w  *bufio.Writer
}

func dial(ctx context.Context, ep endpoint, timeout time.Duration) (*conn, error) {
	d := net.Dialer{Timeout: timeout, KeepAlive: 30 * time.Second}
	var nc net.Conn
	var err error
	if ep.tls {
		td := tls.Dialer{NetDialer: &d, Config: &tls.Config{ServerName: ep.host, MinVersion: tls.VersionTLS12}}
		nc, err = td.DialContext(ctx, "tcp", ep.addr)
	} else {
		nc, err = d.DialContext(ctx, "tcp", ep.addr)
	}
	if err != nil {
		return nil, err
	}
	c := &conn{nc: nc, r: bufio.NewReader(nc), w: bufio.NewWriter(nc)}
	_ = nc.SetDeadline(time.Now().Add(timeout))
	if !ep.password.IsZero() {
		args := []any{"AUTH", ep.password.Reveal()}
		if ep.username != "" {
			args = []any{"AUTH", ep.username, ep.password.Reveal()}
		}
		if _, err := c.command(args); err != nil {
			_ = nc.Close()
			return nil, err
		}
	}
	if ep.db != 0 {
		if _, err := c.command([]any{"SELECT", ep.db}); err != nil {
			_ = nc.Close()
			return nil, err
		}
	}
	_ = nc.SetDeadline(time.Time{})
	return c, nil
}

func (c *conn) close() { _ = c.nc.Close() }

// roundTrip 发一条命令、读一个回复；服务端的错误回复作为 serverError 值返回（连接仍可用）。
func (c *conn) roundTrip(args []any) (any, error) {
	if err := c.write(args); err != nil {
		return nil, err
	}
	return c.read()
}

// command 同 roundTrip，但把服务端的错误回复当成 error。
func (c *conn) command(args []any) (any, error) {
	reply, err := c.roundTrip(args)
	if se, ok := reply.(serverError); ok && err == nil {
		return nil, se
	}
	return reply, err
}

func (c *conn) write(args []any) error {
	fmt.Fprintf(c.w, "*%d\r\n", len(args))
	for _, a := range args {
		var b []byte
		switch v := a.(type) {
		case string:
			b = []byte(v)
		case []byte:
			b = v
		case int:
			b = strconv.AppendInt(nil, int64(v), 10)
		case int64:
			b = strconv.AppendInt(nil, v, 10)
		default:
			return fmt.Errorf("不支持的参数类型 %T", a)
		}
		fmt.Fprintf(c.w, "$%d\r\n", len(b))
		_, _ = c.w.Write(b)
		_, _ = c.w.WriteString("\r\n")
	}
	return c.w.Flush()
}

// read 读一个回复：简单字符串是 string，批量字符串是 []byte，整数是 int64，空是 nil，数组是 []any，错误是 serverError。
// serverError 作为值返回（第二个返回值为 nil 时也可能是它），由 do 统一转成 error。
func (c *conn) read() (any, error) {
	line, err := c.r.ReadSlice('\n')
	if err != nil {
		return nil, err
	}
	if len(line) < 3 || line[len(line)-2] != '\r' {
		return nil, errors.New("RESP 协议错误")
	}
	body := string(line[1 : len(line)-2])
	switch line[0] {
	case '+':
		return body, nil
	case '-':
		return serverError(body), nil
	case ':':
		return strconv.ParseInt(body, 10, 64)
	case '$':
		n, err := strconv.Atoi(body)
		if err != nil {
			return nil, err
		}
		if n < 0 {
			return nil, nil
		}
		buf := make([]byte, n+2)
		if _, err := io.ReadFull(c.r, buf); err != nil {
			return nil, err
		}
		return buf[:n], nil
	case '*':
		n, err := strconv.Atoi(body)
		if err != nil {
			return nil, err
		}
		if n < 0 {
			return nil, nil
		}
		out := make([]any, n)
		for i := range out {
			if out[i], err = c.read(); err != nil {
				return nil, err
			}
		}
		return out, nil
	default:
		return nil, errors.New("RESP 协议错误")
	}
}

// pool 是有上限的连接池。
type pool struct {
	ep      endpoint
	timeout time.Duration
	opTime  time.Duration
	sem     chan struct{}
	idle    chan *conn
}

func newPool(ep endpoint, maxConns int, dialTimeout, opTimeout time.Duration) *pool {
	return &pool{ep: ep, timeout: dialTimeout, opTime: opTimeout, sem: make(chan struct{}, maxConns), idle: make(chan *conn, maxConns)}
}

func (p *pool) get(ctx context.Context) (*conn, error) {
	select {
	case p.sem <- struct{}{}:
	case <-ctx.Done():
		return nil, ctx.Err()
	}
	select {
	case c := <-p.idle:
		return c, nil
	default:
	}
	c, err := dial(ctx, p.ep, p.timeout)
	if err != nil {
		<-p.sem
		return nil, err
	}
	return c, nil
}

func (p *pool) put(c *conn, broken bool) {
	if broken {
		c.close()
	} else {
		select {
		case p.idle <- c:
		default:
			c.close()
		}
	}
	<-p.sem
}

func (p *pool) close() {
	for {
		select {
		case c := <-p.idle:
			c.close()
		default:
			return
		}
	}
}

// deadline 是一次命令的期限：ctx 的期限与默认的命令超时取早的。
func (p *pool) deadline(ctx context.Context, extra time.Duration) time.Time {
	d := time.Now().Add(p.opTime + extra)
	if cd, ok := ctx.Deadline(); ok && cd.Before(d) {
		d = cd
	}
	return d
}

// do 执行一条命令。服务端错误按 serverError 返回（连接仍可用），网络错误时丢弃连接。
func (p *pool) do(ctx context.Context, args ...any) (any, error) {
	c, err := p.get(ctx)
	if err != nil {
		return nil, err
	}
	_ = c.nc.SetDeadline(p.deadline(ctx, 0))
	reply, err := c.roundTrip(args)
	if err != nil {
		p.put(c, true)
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		return nil, err
	}
	p.put(c, false)
	if se, ok := reply.(serverError); ok {
		return nil, se
	}
	return reply, nil
}

// script 是一段 Lua 脚本。
type script struct {
	src string
	sha string
}

func newScript(src string) script {
	sum := sha1.Sum([]byte(src))
	return script{src: src, sha: hex.EncodeToString(sum[:])}
}

// eval 先 EVALSHA，服务端没有缓存这段脚本时回退 EVAL（之后它就缓存了）。
func (p *pool) eval(ctx context.Context, s script, keys []string, args ...any) (any, error) {
	call := func(cmd, body string) (any, error) {
		full := make([]any, 0, 3+len(keys)+len(args))
		full = append(full, cmd, body, len(keys))
		for _, k := range keys {
			full = append(full, k)
		}
		return p.do(ctx, append(full, args...)...)
	}
	reply, err := call("EVALSHA", s.sha)
	var se serverError
	if errors.As(err, &se) && se.prefix() == "NOSCRIPT" {
		return call("EVAL", s.src)
	}
	return reply, err
}
