package openaicompat

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/modelprovider"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/secretstore"
	"github.com/agentruntime/agent-runtime/libs/go-core/secret"
	"github.com/agentruntime/agent-runtime/libs/go-core/tenancy"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

// DefaultTimeout 是 Options.Timeout 为 0 时每次调用的超时。
const DefaultTimeout = 120 * time.Second

const (
	chatPath  = "/chat/completions"
	embedPath = "/embeddings"
	// platformPurpose 是读平台密钥时 SecretStore 平台作用域的用途。
	platformPurpose = "model-provider"
	// maxErrorBody 是读取上游错误正文的上限。
	maxErrorBody = 64 << 10
	// maxQuoted 是错误信息里引用上游 error.message 的最大字符数。
	maxQuoted = 200
)

// Options 是构造参数。
type Options struct {
	// BaseURL 是 OpenAI 兼容 API 的根地址，形如 https://host/v1。
	BaseURL string
	// Secrets 是读取 API Key 的 SecretStore。
	Secrets secretstore.Provider
	// KeyName 是 SecretStore 里的密钥名（租户作用域与平台作用域用同一个名字）。
	KeyName string
	// HTTPClient 为 nil 时用适配层自己的客户端。
	HTTPClient *http.Client
	// Timeout 是每次调用的超时；为 0 时用 DefaultTimeout。流式调用从发出请求到读完最后一个分片都算在内。
	Timeout time.Duration
}

// Provider 是 OpenAI 兼容适配层的工厂，长期存在、可并发使用。不持有密钥：每次调用经 SecretStore 读取。
type Provider struct {
	base    string
	secrets secretstore.Provider
	keyName string
	http    *http.Client
	timeout time.Duration
	models  modelList
}

// modelListTTL 是模型列表的缓存时长。
const modelListTTL = 5 * time.Minute

// modelList 缓存 GET /models 的结果，按密钥的哈希分开（BYOK 的租户看到的模型可能不同；不保存密钥本身）。
// 只在上游回了笼统的 400 时才查：模型确实不在列表里，就把错误改成 not_found。
type modelList struct {
	mu      sync.Mutex
	entries map[[32]byte]modelListEntry
}

type modelListEntry struct {
	at  time.Time
	ids map[string]bool
}

// unlisted 判断 model 是否确实不在上游的模型列表里；列表取不到时返回 false（不改变原来的分类）。
func (m *modelList) unlisted(ctx context.Context, p *Provider, key secret.Value, model string) bool {
	hash := sha256.Sum256(key.Reveal())
	m.mu.Lock()
	entry, ok := m.entries[hash]
	m.mu.Unlock()
	if !ok || time.Since(entry.at) > modelListTTL {
		ids, err := p.listModels(ctx, key)
		if err != nil {
			return false
		}
		entry = modelListEntry{at: time.Now(), ids: ids}
		m.mu.Lock()
		if m.entries == nil {
			m.entries = map[[32]byte]modelListEntry{}
		}
		m.entries[hash] = entry
		m.mu.Unlock()
	}
	return !entry.ids[model]
}

// listModels 读 GET /models。
func (p *Provider) listModels(ctx context.Context, key secret.Value) (map[string]bool, error) {
	ctx, cancel := context.WithTimeout(ctx, p.timeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, p.base+"/models", nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+key.RevealString())
	resp, err := p.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, errors.New("模型列表不可用")
	}
	var list struct {
		Data []struct {
			ID string `json:"id"`
		} `json:"data"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 4<<20)).Decode(&list); err != nil || len(list.Data) == 0 {
		return nil, errors.New("模型列表不可用")
	}
	ids := make(map[string]bool, len(list.Data))
	for _, d := range list.Data {
		ids[d.ID] = true
	}
	return ids, nil
}

// New 构造适配层。BaseURL 必须是 http 或 https 的绝对地址，Secrets 与 KeyName 必填；不合法时返回错误。
func New(opts Options) (*Provider, error) {
	if opts.Secrets == nil {
		return nil, errors.New("openaicompat：缺少 Secrets")
	}
	if strings.TrimSpace(opts.KeyName) == "" {
		return nil, errors.New("openaicompat：缺少 KeyName")
	}
	raw := strings.TrimSpace(opts.BaseURL)
	u, err := url.Parse(raw)
	if raw == "" || err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return nil, errors.New("openaicompat：BaseURL 必须是 http 或 https 的绝对地址")
	}
	httpClient := opts.HTTPClient
	if httpClient == nil {
		httpClient = &http.Client{Transport: http.DefaultTransport.(*http.Transport).Clone()}
	}
	timeout := opts.Timeout
	if timeout <= 0 {
		timeout = DefaultTimeout
	}
	return &Provider{base: strings.TrimRight(raw, "/"), secrets: opts.Secrets, keyName: opts.KeyName, http: httpClient, timeout: timeout}, nil
}

// Bind 得到绑定租户的客户端：凭据优先用租户的 BYOK 密钥，没有时用平台密钥。
func (p *Provider) Bind(tenant *commonv1.TenantContext) (modelprovider.Client, error) {
	scope, err := tenancy.TenantScope(tenant)
	if err != nil {
		return nil, err
	}
	return &client{p: p, scope: scope}, nil
}

// BindPlatform 得到平台作用域的客户端（例如离线评测），只用平台密钥；purpose 必填。
func (p *Provider) BindPlatform(purpose string) (modelprovider.Client, error) {
	scope, err := tenancy.PlatformScope(purpose)
	if err != nil {
		return nil, err
	}
	return &client{p: p, scope: scope}, nil
}

// Close 释放空闲连接。
func (p *Provider) Close() error {
	p.http.CloseIdleConnections()
	return nil
}

// Format 让各种打印形式只给出端点（Provider 本来就不持有密钥，这里再保险一层）。
func (p *Provider) Format(f fmt.State, _ rune) {
	_, _ = fmt.Fprintf(f, "openaicompat.Provider(%s)", p.base)
}

// EffortForBudget 把思考预算（token）按区间映射到 reasoning_effort：
// 0 → ""（不发送该字段）；1–4096 → "low"；4097–16384 → "medium"；更大 → "high"。
func EffortForBudget(thinkingBudgetTokens int) string {
	switch {
	case thinkingBudgetTokens <= 0:
		return ""
	case thinkingBudgetTokens <= 4096:
		return "low"
	case thinkingBudgetTokens <= 16384:
		return "medium"
	default:
		return "high"
	}
}

// client 是绑定了作用域的客户端。不缓存密钥：每次调用重新读取，密钥轮换与吊销立即生效。
type client struct {
	p     *Provider
	scope tenancy.Scope
}

func (c *client) Format(f fmt.State, _ rune) {
	scope := "platform:" + c.scope.Purpose
	if !c.scope.IsPlatform() {
		scope = "tenant:" + c.scope.TenantID
	}
	_, _ = fmt.Fprintf(f, "openaicompat.client(%s)", scope)
}

// apiKey 按作用域读密钥：租户作用域先读 BYOK，NotFound 时再读平台密钥；其他错误照原类别返回（不退回平台密钥，
// 否则租户的调用会记到平台的账上）。两处都没有：Denied。
func (c *client) apiKey(ctx context.Context) (secret.Value, error) {
	if !c.scope.IsPlatform() {
		tenant, err := c.scope.TenantContext()
		if err != nil {
			return secret.Value{}, err
		}
		store, err := c.p.secrets.Bind(tenant)
		v, err := readSecret(ctx, store, err, c.p.keyName)
		if err == nil {
			return v, nil
		}
		if providererr.CategoryOf(err) != providererr.NotFound {
			return secret.Value{}, err
		}
	}
	store, err := c.p.secrets.BindPlatform(platformPurpose)
	v, err := readSecret(ctx, store, err, c.p.keyName)
	if providererr.CategoryOf(err) == providererr.NotFound {
		return secret.Value{}, providererr.New(providererr.Denied, "missing_api_key", "没有可用的模型供应商密钥")
	}
	return v, err
}

// readSecret 读一个密钥；空值按 NotFound；不是 *providererr.Error 的错误按 Transient（不带原文）。
func readSecret(ctx context.Context, store secretstore.Store, bindErr error, name string) (secret.Value, error) {
	if bindErr != nil {
		return secret.Value{}, asProviderError(bindErr)
	}
	v, err := store.Read(ctx, name)
	if err != nil {
		return secret.Value{}, asProviderError(err)
	}
	if v.IsZero() {
		return secret.Value{}, providererr.New(providererr.NotFound, "not_found", "密钥为空")
	}
	return v, nil
}

func asProviderError(err error) error {
	var pe *providererr.Error
	if errors.As(err, &pe) {
		return pe
	}
	return providererr.New(providererr.Transient, "secret_store", "读取模型供应商密钥失败")
}

// response 是一次成功（2xx）的上游响应；调用方读完 body 后调用 done。
type response struct {
	body io.ReadCloser
	done context.CancelFunc
	// ctx 是本次调用（含超时）的上下文，读 body 出错时据此区分超时。
	ctx context.Context
}

// post 向上游发一个 POST（不重试）。2xx 返回响应；其余状态与传输错误分类成 *providererr.Error。model 是请求的模型名，
// 用来把笼统的 400 区分成 not_found（见 modelList）。accept 按请求的种类给：非流式只接受 application/json——有的聚合网关
// 看到 Accept 里有 text/event-stream，会把非流式请求（包括它的拒绝）当流返回（T-13 真模型端到端发现）。
func (c *client) post(ctx context.Context, path, model, accept string, payload any) (*response, error) {
	key, err := c.apiKey(ctx)
	if err != nil {
		return nil, err
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return nil, providererr.New(providererr.Invalid, "bad_request", "请求无法编码成 JSON")
	}
	ctx, cancel := context.WithTimeout(ctx, c.p.timeout)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.p.base+path, bytes.NewReader(body))
	if err != nil {
		cancel()
		return nil, providererr.New(providererr.Invalid, "bad_request", "无法构造请求")
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", accept)
	req.Header.Set("Authorization", "Bearer "+key.RevealString())
	resp, err := c.p.http.Do(req)
	if err != nil {
		cancel()
		return nil, transportError(ctx, err)
	}
	if resp.StatusCode/100 != 2 {
		raw, _ := io.ReadAll(io.LimitReader(resp.Body, maxErrorBody))
		_ = resp.Body.Close()
		defer cancel()
		e := classify(resp.StatusCode, resp.Header, raw, key)
		if e.Category == providererr.Invalid && model != "" && c.p.models.unlisted(ctx, c.p, key, model) {
			// 有的聚合网关对未知模型只回笼统的 400：查一下模型列表，确实没有就是 not_found。
			return nil, providererr.New(providererr.NotFound, "model_not_found", "模型供应商没有这个模型："+model)
		}
		return nil, e
	}
	return &response{body: resp.Body, done: cancel, ctx: ctx}, nil
}

// postJSON 发请求并把响应体解析进 out；解析失败为 Transient（上游的临时故障）。
func (c *client) postJSON(ctx context.Context, path, model string, payload, out any) error {
	r, err := c.post(ctx, path, model, "application/json", payload)
	if err != nil {
		return err
	}
	defer r.done()
	defer r.body.Close()
	raw, err := io.ReadAll(r.body)
	if err != nil {
		return transportError(r.ctx, err)
	}
	if err := json.Unmarshal(raw, out); err != nil {
		return providererr.New(providererr.Transient, "bad_response", "上游的响应不是合法 JSON")
	}
	return nil
}

// transportError：超时（含调用方的截止时间）与网络错误都是 Transient。底层错误只作原因保留，不进错误文本。
func transportError(ctx context.Context, err error) error {
	if errors.Is(err, context.DeadlineExceeded) || errors.Is(ctx.Err(), context.DeadlineExceeded) {
		return providererr.Wrap(providererr.Transient, "timeout", "调用模型供应商超时", err)
	}
	if errors.Is(err, context.Canceled) {
		return providererr.Wrap(providererr.Transient, "canceled", "调用被取消", err)
	}
	return providererr.Wrap(providererr.Transient, "unreachable", "连不上模型供应商或连接中断", err)
}

var (
	upstreamCode = regexp.MustCompile(`^[A-Za-z0-9_.-]{1,64}$`)
	bearerToken  = regexp.MustCompile(`(?i)bearer\s+\S+`)
	imageDataURL = regexp.MustCompile(`(?i)data:image/[a-z0-9.+-]+;base64,[a-z0-9+/=\r\n]*`)
)

// classify 把上游的非 2xx 响应分类（规则见包注释）。只引用 JSON 正文里的 error.message，先去掉密钥。
func classify(status int, header http.Header, raw []byte, key secret.Value) *providererr.Error {
	var body struct {
		Error struct {
			Message string `json:"message"`
			Code    any    `json:"code"`
		} `json:"error"`
	}
	message, code := "", ""
	if json.Unmarshal(raw, &body) == nil {
		message = sanitize(body.Error.Message, key)
		if s, ok := body.Error.Code.(string); ok && upstreamCode.MatchString(s) && !strings.Contains(key.RevealString(), s) {
			code = s
		}
	}
	text := fmt.Sprintf("模型供应商返回 %d", status)
	if message != "" {
		text += "：" + message
	}
	notFound := code == "model_not_found"
	if code == "" {
		code = "http_" + strconv.Itoa(status)
	}
	switch {
	case status >= 500:
		return providererr.New(providererr.Transient, code, text)
	case status == http.StatusNotFound || (status/100 == 4 && notFound):
		return providererr.New(providererr.NotFound, code, text)
	case status == http.StatusUnauthorized || status == http.StatusForbidden:
		return providererr.New(providererr.Denied, code, text)
	case status == http.StatusTooManyRequests:
		e := providererr.New(providererr.Transient, code, text)
		e.RetryAfter = retryAfter(header.Get("Retry-After"))
		return e
	case status == http.StatusRequestTimeout:
		return providererr.New(providererr.Transient, code, text)
	case status/100 == 4:
		return providererr.New(providererr.Invalid, code, text)
	default:
		return providererr.New(providererr.Transient, code, text)
	}
}

// sanitize 去掉文本里的密钥与 Bearer 凭据，截到 maxQuoted 个字符。
func sanitize(s string, key secret.Value) string {
	if k := key.RevealString(); k != "" {
		s = strings.ReplaceAll(s, k, secret.Mask)
	}
	s = bearerToken.ReplaceAllString(s, "Bearer "+secret.Mask)
	// Providers sometimes echo invalid image payloads in error.message. Never
	// propagate those bytes into gateway errors, traces or metering logs.
	s = imageDataURL.ReplaceAllString(s, "[image data redacted]")
	if r := []rune(s); len(r) > maxQuoted {
		s = string(r[:maxQuoted]) + "…"
	}
	return strings.TrimSpace(s)
}

// retryAfter 解析 Retry-After：秒数或 HTTP 日期；解析不了为 0。
func retryAfter(v string) time.Duration {
	v = strings.TrimSpace(v)
	if v == "" {
		return 0
	}
	if secs, err := strconv.ParseFloat(v, 64); err == nil && secs > 0 {
		return time.Duration(secs * float64(time.Second))
	}
	if at, err := http.ParseTime(v); err == nil {
		if d := time.Until(at); d > 0 {
			return d
		}
	}
	return 0
}
