package openaicompat_test

// 验收“真实端点（设了 AR_LIVE_MODEL_PROVIDER=1 才跑）：适配层一致性测试在真实端点上通过”。
//
// 端点与 Key 只从环境变量读：AR_MODEL_PROVIDER_BASE_URL、AR_MODEL_PROVIDER_API_KEY（由人自己 source 本地密钥文件，
// 不写进仓库）；模型名：AR_LIVE_MODEL（默认 gpt-4o-mini）、AR_LIVE_EMBEDDING_MODEL（默认 text-embedding-3-small）。
// Key 只放在内存里的 SecretStore（memSecrets），不落盘。真实端点无法注入故障、无法按 Key 反推调用记录，
// 对应用例返回 conformance.ErrUnsupported 跳过。
//
// 错误先查过不含真实 Key 才打印（assertNoLiveKey 在前），免得实现有缺陷时把 Key 打进测试输出。

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/openaicompat"
	"github.com/agentruntime/agent-runtime/libs/go-core/conformance"
	"github.com/agentruntime/agent-runtime/libs/go-core/conformance/modelprovidertest"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/modelprovider"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/secretstore"
	"github.com/agentruntime/agent-runtime/libs/go-core/secret"
	"github.com/agentruntime/agent-runtime/libs/go-core/tenancy"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

type liveConfig struct {
	baseURL, key, model, embeddingModel string
}

// liveOrSkip 读真实端点的配置；没设 AR_LIVE_MODEL_PROVIDER=1 时跳过，设了但缺端点或 Key 时失败。
func liveOrSkip(t *testing.T) liveConfig {
	t.Helper()
	if os.Getenv("AR_LIVE_MODEL_PROVIDER") != "1" {
		t.Skip("真实端点测试只在 AR_LIVE_MODEL_PROVIDER=1 时运行（还要设 AR_MODEL_PROVIDER_BASE_URL 与 AR_MODEL_PROVIDER_API_KEY）")
	}
	cfg := liveConfig{
		baseURL:        os.Getenv("AR_MODEL_PROVIDER_BASE_URL"),
		key:            os.Getenv("AR_MODEL_PROVIDER_API_KEY"),
		model:          envOr("AR_LIVE_MODEL", "gpt-4o-mini"),
		embeddingModel: envOr("AR_LIVE_EMBEDDING_MODEL", "text-embedding-3-small"),
	}
	if cfg.baseURL == "" || cfg.key == "" {
		t.Fatal("设了 AR_LIVE_MODEL_PROVIDER=1，但缺 AR_MODEL_PROVIDER_BASE_URL 或 AR_MODEL_PROVIDER_API_KEY")
	}
	return cfg
}

func envOr(name, fallback string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	return fallback
}

// liveProvider 构造连真实端点的适配层；client 非空时作为 Options.HTTPClient。
func liveProvider(t *testing.T, cfg liveConfig, platformKey string, client *http.Client) *openaicompat.Provider {
	t.Helper()
	secrets := &memSecrets{platform: map[string]string{keyName: platformKey}}
	return newProvider(t, openaicompat.Options{BaseURL: cfg.baseURL, Secrets: secrets, KeyName: keyName, HTTPClient: client, Timeout: 90 * time.Second})
}

func livePlatform(t *testing.T, p *openaicompat.Provider) modelprovider.Client {
	t.Helper()
	c, err := p.BindPlatform("live-test")
	if err != nil {
		t.Fatalf("BindPlatform: %v", err)
	}
	return c
}

type liveHarness struct {
	provider              *openaicompat.Provider
	model, embeddingModel string
}

func (h *liveHarness) Provider() modelprovider.Provider { return h.provider }
func (h *liveHarness) Model() string                    { return h.model }
func (h *liveHarness) EmbeddingModel() string           { return h.embeddingModel }
func (h *liveHarness) InjectFault(string, providererr.Category) error {
	return conformance.ErrUnsupported
}
func (h *liveHarness) Calls() ([]modelprovidertest.RecordedCall, error) {
	return nil, conformance.ErrUnsupported
}

// TestLiveConformance：在真实端点上跑 modelprovidertest 一致性测试。
func TestLiveConformance(t *testing.T) {
	cfg := liveOrSkip(t)
	modelprovidertest.Run(t, func(t *testing.T) modelprovidertest.Harness {
		return &liveHarness{provider: liveProvider(t, cfg, cfg.key, nil), model: cfg.model, embeddingModel: cfg.embeddingModel}
	})
}

// TestLiveErrorsAreClassifiedWithoutLeakingKey：真实端点上，未知模型与不被认可的 Key 两种错误的文本与错误链里都不出现 Key；
// 不被认可的 Key 为 Denied；未知模型的类别与上游实际返回的状态码一致（状态码 404 或 4xx + model_not_found 为 NotFound；
// 5xx 为 Transient，见 openaicompat.go 的错误分类）。上游的状态码与 error.code 记进测试日志，供人确认 503 + model_not_found
// 的分类（评审意见：聚合网关用它表示“暂时没有可用渠道”）。
func TestLiveErrorsAreClassifiedWithoutLeakingKey(t *testing.T) {
	cfg := liveOrSkip(t)
	ctx := context.Background()

	rec := &upstreamRecorder{}
	c := livePlatform(t, liveProvider(t, cfg, cfg.key, &http.Client{Transport: rec}))
	_, err := c.Generate(ctx, ask("no-such-model-ar-live-test", "x"))
	assertNoLiveKey(t, err, cfg.key)
	status, code, typ := rec.last(cfg.key)
	t.Logf("真实端点对未知模型的响应：HTTP %d，error.code=%q，error.type=%q", status, code, typ)
	switch {
	case status == http.StatusNotFound || (status >= 400 && status < 500 && code == "model_not_found"):
		conformance.ExpectError(t, err, providererr.NotFound, "")
	case status >= 500:
		t.Logf("未知模型按 5xx 返回：按约定为 transient（Model Gateway 会重试并切换同档的 Profile）；" +
			"Model Profile 里写错的模型名不会表现为 not_found，要靠计量记录与日志发现。这一分类待人确认")
		conformance.ExpectError(t, err, providererr.Transient, "")
	case status == http.StatusBadRequest:
		// 2026-10-21 实测：a6api 对未知模型只回笼统的 400（error.code 为 invalid_request）。适配层这时查 GET /models，
		// 模型确实不在列表里就报 not_found（见 provider.go 的 modelList）。
		conformance.ExpectError(t, err, providererr.NotFound, "model_not_found")
	default:
		t.Fatalf("真实端点对未知模型返回 HTTP %d（error.code=%q），不在约定的分类里，需要人决定如何映射", status, code)
	}

	rec = &upstreamRecorder{}
	bogus := livePlatform(t, liveProvider(t, cfg, "sk-test-DO-NOT-LEAK-invalid-0000", &http.Client{Transport: rec}))
	_, err = bogus.Generate(ctx, ask(cfg.model, "x"))
	assertNoLiveKey(t, err, cfg.key)
	assertNoSecret(t, err, "真实端点不认的 Key")
	status, code, typ = rec.last(cfg.key)
	t.Logf("真实端点对不认的 Key 的响应：HTTP %d，error.code=%q，error.type=%q", status, code, typ)
	conformance.ExpectError(t, err, providererr.Denied, "")
}

// liveProfile 是 Model Profile 文件里本测试用到的字段（格式见 docs/plan/model-gateway-direct.md 与 deploy/dev/model-profiles.json）。
type liveProfile struct {
	ID     string `json:"id"`
	Vendor string `json:"vendor"`
	Model  string `json:"model"`
	Class  string `json:"class"`
	Tiers  []struct {
		Name string `json:"name"`
	} `json:"tiers"`
	Capabilities []string `json:"capabilities"`
}

// judgeProfiles 读 Model Profile 文件（AR_MODEL_PROFILES_FILE；没设时用仓库里的 deploy/dev/model-profiles.json），
// 返回 judge 的默认 Tier strong@medium（附录 H.3）里能做结构化输出的全部 Profile，按 id 排序。
func judgeProfiles(t *testing.T) []liveProfile {
	t.Helper()
	path := envOr("AR_MODEL_PROFILES_FILE", filepath.Join("..", "..", "..", "..", "deploy", "dev", "model-profiles.json"))
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("读 Model Profile 文件 %s：%v", path, err)
	}
	var file struct {
		Profiles []liveProfile `json:"profiles"`
	}
	if err := json.Unmarshal(raw, &file); err != nil {
		t.Fatalf("Model Profile 文件 %s 不是合法的 JSON：%v", path, err)
	}
	var out []liveProfile
	for _, p := range file.Profiles {
		hasMedium := slices.ContainsFunc(p.Tiers, func(tier struct {
			Name string `json:"name"`
		}) bool {
			return tier.Name == "medium"
		})
		if p.Class == "strong" && hasMedium && slices.Contains(p.Capabilities, "structured") {
			out = append(out, p)
		}
	}
	slices.SortFunc(out, func(a, b liveProfile) int { return strings.Compare(a.ID, b.ID) })
	if len(out) == 0 {
		t.Fatalf("Model Profile 文件 %s 里没有 strong@medium 且能做结构化输出的 Profile", path)
	}
	return out
}

// TestLiveScoreOnJudgeProfiles：评审意见“Score 的 schema（严格模式下的 minItems、maxItems）在真实端点背后各家模型上未经验证”。
// 对 Model Profile 文件里 strong@medium（judge 的默认 Tier）的每个 Profile（每一家都在内）各跑一次 Score：
// 不被拒绝（400 时提示去掉 minItems、maxItems）、分数个数等于候选个数、都是有限数、明显最相关的候选得分最高（第 i 个分数对应第 i 个候选）。
func TestLiveScoreOnJudgeProfiles(t *testing.T) {
	cfg := liveOrSkip(t)
	profiles := judgeProfiles(t)
	c := livePlatform(t, liveProvider(t, cfg, cfg.key, nil))
	vendors := map[string]bool{}
	for _, p := range profiles {
		vendors[p.Vendor] = true
		t.Run(p.ID, func(t *testing.T) {
			r, err := c.Score(context.Background(), modelprovider.ScoreRequest{
				Model:  p.Model,
				Query:  "猫通常怎么叫？",
				Rubric: "候选对这个问题回答得是否正确：完全正确给 1，完全无关或错误给 0，可以给中间值",
				Candidates: []string{
					"今天上证指数小幅上涨。",
					"猫通常发出“喵喵”的叫声。",
					"猫通常像狗一样“汪汪”地叫。",
				},
			})
			if err != nil {
				assertNoLiveKey(t, err, cfg.key)
				if providererr.CategoryOf(err) == providererr.Invalid {
					t.Fatalf("%s（vendor %s）拒绝了 Score 的请求：%v。可能是严格模式下不接受 minItems、maxItems："+
						"按评审意见去掉这两个关键字，只靠适配层自己检查分数个数（见 openaicompat.go 的 Score 一节）", p.ID, p.Vendor, err)
				}
				t.Fatalf("%s（vendor %s）：Score：%v", p.ID, p.Vendor, err)
			}
			t.Logf("%s（vendor %s）：scores = %v", p.ID, p.Vendor, r.Scores)
			if len(r.Scores) != 3 {
				t.Fatalf("分数个数 = %d，want 3", len(r.Scores))
			}
			for i, s := range r.Scores {
				if math.IsNaN(s) || math.IsInf(s, 0) {
					t.Fatalf("Scores[%d] = %v 不是有限数", i, s)
				}
			}
			if !(r.Scores[1] > r.Scores[0] && r.Scores[1] > r.Scores[2]) {
				t.Errorf("明显最相关的第 2 个候选应得分最高（分数与候选按顺序对应）：%v", r.Scores)
			}
		})
	}
	t.Logf("strong@medium 覆盖的 vendor：%v", slices.Sorted(func(yield func(string) bool) {
		for v := range vendors {
			if !yield(v) {
				return
			}
		}
	}))
}

// upstreamRecorder 是 Options.HTTPClient 的 Transport：记下最近一次 POST 的上游响应的状态码，以及错误正文里的 error.code 与
// error.type（适配层随后查模型列表的 GET 不算）。不记 error.message（上游可能在里面回显 Key）。
type upstreamRecorder struct {
	mu        sync.Mutex
	status    int
	code, typ string
}

func (u *upstreamRecorder) RoundTrip(r *http.Request) (*http.Response, error) {
	resp, err := http.DefaultTransport.RoundTrip(r)
	if err != nil || r.Method != http.MethodPost {
		return resp, err
	}
	var code, typ string
	if resp.StatusCode >= 400 {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
		_ = resp.Body.Close()
		resp.Body = io.NopCloser(bytes.NewReader(body))
		var parsed struct {
			Error struct {
				Code any `json:"code"`
				Type any `json:"type"`
			} `json:"error"`
		}
		if json.Unmarshal(body, &parsed) == nil {
			code, typ = scalar(parsed.Error.Code), scalar(parsed.Error.Type)
		}
	}
	u.mu.Lock()
	u.status, u.code, u.typ = resp.StatusCode, code, typ
	u.mu.Unlock()
	return resp, nil
}

// last 返回最近一次的状态码、error.code、error.type；含 Key 的字段换成占位，免得打印出来。
func (u *upstreamRecorder) last(key string) (int, string, string) {
	u.mu.Lock()
	defer u.mu.Unlock()
	clean := func(s string) string {
		if containsSecret(s) || (key != "" && strings.Contains(s, key)) || len(s) > 64 {
			return "<略>"
		}
		return s
	}
	return u.status, clean(u.code), clean(u.typ)
}

func scalar(v any) string {
	switch x := v.(type) {
	case nil:
		return ""
	case string:
		return x
	default:
		return fmt.Sprint(x)
	}
}

// assertNoLiveKey 断言错误链里没有真实 Key；失败时不打印错误的内容。
func assertNoLiveKey(t *testing.T, err error, key string) {
	t.Helper()
	for e := err; e != nil; {
		for _, text := range []string{e.Error(), fmt.Sprintf("%+v", e), fmt.Sprintf("%#v", e)} {
			if strings.Contains(text, key) {
				t.Fatal("错误里出现了真实 Key（不打印内容）")
			}
		}
		u, ok := e.(interface{ Unwrap() error })
		if !ok {
			break
		}
		e = u.Unwrap()
	}
}

// memSecrets 是只放在内存里的 SecretStore，只实现 Read（真实 Key 不落盘）。
type memSecrets struct {
	platform map[string]string
	tenants  map[string]map[string]string
}

func (m *memSecrets) Bind(t *commonv1.TenantContext) (secretstore.Store, error) {
	s, err := tenancy.TenantScope(t)
	if err != nil {
		return nil, err
	}
	return memStore{values: m.tenants[s.TenantID]}, nil
}

func (m *memSecrets) BindPlatform(purpose string) (secretstore.Store, error) {
	if _, err := tenancy.PlatformScope(purpose); err != nil {
		return nil, err
	}
	return memStore{values: m.platform}, nil
}

func (m *memSecrets) Close() error { return nil }

type memStore struct{ values map[string]string }

var errMemUnsupported = providererr.New(providererr.Invalid, "unsupported", "测试用的内存 SecretStore 只支持 Read")

func (s memStore) Read(_ context.Context, name string) (secret.Value, error) {
	v, ok := s.values[name]
	if !ok {
		return secret.Value{}, providererr.New(providererr.NotFound, "not_found", "密钥不存在")
	}
	return secret.New(v), nil
}

func (s memStore) IssueCredential(context.Context, string, time.Duration) (secretstore.Credential, error) {
	return secretstore.Credential{}, errMemUnsupported
}

func (s memStore) LeaseStatus(context.Context, string) (secretstore.LeaseStatus, error) {
	return "", errMemUnsupported
}

func (s memStore) Revoke(context.Context, string) error { return errMemUnsupported }

func (s memStore) GenerateDataKey(context.Context, string) (secretstore.DataKey, error) {
	return secretstore.DataKey{}, errMemUnsupported
}

func (s memStore) UnwrapDataKey(context.Context, []byte, string) (secret.Value, error) {
	return secret.Value{}, errMemUnsupported
}
