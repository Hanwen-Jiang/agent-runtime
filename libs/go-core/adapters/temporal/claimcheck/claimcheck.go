// Package claimcheck 是 Claim-Check：Temporal 大载荷卸载到 ObjectStore，事件历史里只留引用
// （设计书 4.7、附录 K.3、L.8；计划 S-06）。
//
// 两种语言共用同一种格式：Python 侧见 libs/py-core/src/agentruntime_core/adapters/claim_check.py，
// 测试向量见 tests/contract/fixtures/claimcheck/vector.json。
//
// 格式（版本 1）：
//   - 只卸载确定性序列化后超过阈值的载荷（默认 64 KiB，可配置）；不超过的原样保留。
//   - 对象键：claim-check/<原载荷 sha256 十六进制>，在租户自己的 ObjectStore 作用域里（obj://<租户>/…）。
//     内容寻址：同一载荷再编码（例如重放）复用已有对象，不重复上传、不重复生成数据密钥。
//   - 对象内容："ARCC" | u8 版本 | u32 头部长度（大端）| 头部 JSON（alg、key、nonce）| 密文。
//     每个对象一把数据密钥（SecretStore.GenerateDataKey("claim_check")，按租户包装），AES-256-GCM 加密，
//     AAD = "agentruntime/claim-check/v1" 0x00 租户 ID 0x00 对象键。
//   - 引用：metadata {"encoding": "binary/agentruntime-claim-check", "agentruntime-claim-check-version": "1"}，
//     data 为 JSON {"sha256", "size", "uri", "v": 1}。版本号为以后迁移到 Temporal External Storage 留出路径。
//
// 租户：TenantCodec 绑定一个租户，解码别的租户的引用一律 Denied（引用的租户、ObjectStore 的作用域、数据密钥的作用域
// 三处都核对）。接入 Temporal 时：工作流里按工作流 memo 的作用域（agentruntime_scope）确定租户；客户端调用按
// WithTenant 放进 context 的租户（来自认证身份）；其余（activity 等）按所属工作流 ID 的根 describe 出 memo
// （NewMemoResolver）。确定不了租户时拒绝。
//
// 错误码与 Python 侧相同：claim_check_cross_tenant、claim_check_no_tenant（Denied）；claim_check_key_mismatch、
// claim_check_decrypt_failed、claim_check_digest_mismatch、claim_check_bad_reference（Invalid）；
// claim_check_missing（NotFound）；claim_check_unavailable（Transient，本地重试之后）。错误信息带 uri，不带密钥与载荷。
package claimcheck

import (
	"context"
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	commonpb "go.temporal.io/api/common/v1"
	"go.temporal.io/api/serviceerror"
	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/converter"
	"go.temporal.io/sdk/workflow"
	"google.golang.org/protobuf/proto"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/objectstore"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/secretstore"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

// 格式常量，与 Python 侧逐一相同。
const (
	DefaultThreshold       = 64 << 10
	Encoding               = "binary/agentruntime-claim-check"
	VersionKey             = "agentruntime-claim-check-version"
	Version                = 1
	KeyPrefix              = "claim-check/"
	DataKeyPurpose         = "claim_check"
	Algorithm              = "AES-256-GCM"
	ContentType            = "application/vnd.agentruntime.claim-check"
	DefaultLifecycleMargin = 7 * 24 * time.Hour
)

const (
	magic     = "ARCC"
	aadPrefix = "agentruntime/claim-check/v1\x00"
	nonceSize = 12
	// scopeMemoKey 与 temporaladapter.ScopeMemoKey 相同（这里不引用它，免得两个包互相依赖）。
	scopeMemoKey = "agentruntime_scope"
	platformID   = "_platform"
)

var (
	uriPattern    = regexp.MustCompile(`^obj://([a-z0-9][a-z0-9_-]{0,62})/(claim-check/[0-9a-f]{64})$`)
	sha256Pattern = regexp.MustCompile(`^[0-9a-f]{64}$`)
)

// Options 是构造参数。Threshold 为 0 时取 DefaultThreshold；Attempts 为 0 时取 3；Backoff 为 0 时取 200ms。
type Options struct {
	Objects   objectstore.Provider
	Secrets   secretstore.Provider
	Threshold int
	Attempts  int
	Backoff   time.Duration
}

// Reference 是解析后的引用。
type Reference struct {
	Version int
	URI     string
	SHA256  string
	Size    int64
}

// SealedHeader 是对象内容的头部。
type SealedHeader struct {
	Version    int
	Algorithm  string
	WrappedKey []byte
	Nonce      []byte
}

// ClaimCheck 是长期存在的工厂。
type ClaimCheck struct{ opts Options }

// New 校验参数并构造。
func New(opts Options) (*ClaimCheck, error) {
	if opts.Objects == nil || opts.Secrets == nil {
		return nil, errors.New("claimcheck: Objects 与 Secrets 都不能为空")
	}
	if opts.Threshold < 0 || opts.Attempts < 0 || opts.Backoff < 0 {
		return nil, errors.New("claimcheck: Threshold、Attempts、Backoff 不能是负数")
	}
	if opts.Threshold == 0 {
		opts.Threshold = DefaultThreshold
	}
	if opts.Attempts == 0 {
		opts.Attempts = 3
	}
	if opts.Backoff == 0 {
		opts.Backoff = 200 * time.Millisecond
	}
	return &ClaimCheck{opts: opts}, nil
}

// Threshold 返回阈值（字节）。
func (c *ClaimCheck) Threshold() int { return c.opts.Threshold }

// TenantCodec 是绑定了一个租户的编解码。
type TenantCodec struct {
	cc       *ClaimCheck
	tenantID string
	objects  objectstore.Store
	secrets  secretstore.Store
}

// Bind 绑定租户。
func (c *ClaimCheck) Bind(tenant *commonv1.TenantContext) (*TenantCodec, error) {
	objects, err := c.opts.Objects.Bind(tenant)
	if err != nil {
		return nil, err
	}
	secrets, err := c.opts.Secrets.Bind(tenant)
	if err != nil {
		return nil, err
	}
	return &TenantCodec{cc: c, tenantID: tenant.GetTenantId(), objects: objects, secrets: secrets}, nil
}

// Encode 把超过阈值的载荷换成引用，其余原样保留，顺序不变。
func (t *TenantCodec) Encode(ctx context.Context, payloads []*commonpb.Payload) ([]*commonpb.Payload, error) {
	out := make([]*commonpb.Payload, len(payloads))
	for i, p := range payloads {
		plain, err := Serialize(p)
		if err != nil {
			return nil, err
		}
		if len(plain) <= t.cc.opts.Threshold {
			out[i] = p
			continue
		}
		if out[i], err = t.offload(ctx, plain); err != nil {
			return nil, err
		}
	}
	return out, nil
}

// Decode 把引用换回原载荷，其余原样保留。
func (t *TenantCodec) Decode(ctx context.Context, payloads []*commonpb.Payload) ([]*commonpb.Payload, error) {
	out := make([]*commonpb.Payload, len(payloads))
	for i, p := range payloads {
		if !IsReference(p) {
			out[i] = p
			continue
		}
		var err error
		if out[i], err = t.restore(ctx, p); err != nil {
			return nil, err
		}
	}
	return out, nil
}

func (t *TenantCodec) offload(ctx context.Context, plain []byte) (*commonpb.Payload, error) {
	sum := sha256.Sum256(plain)
	digest := hex.EncodeToString(sum[:])
	key := KeyPrefix + digest
	uri := t.objects.URI(key)
	exists, err := retry(ctx, t.cc, "ObjectStore", uri, func() (bool, error) {
		_, err := t.objects.Head(ctx, key)
		if providererr.CategoryOf(err) == providererr.NotFound {
			return false, nil
		}
		return err == nil, err
	})
	if err != nil {
		return nil, err
	}
	if !exists {
		dk, err := retry(ctx, t.cc, "SecretStore", uri, func() (secretstore.DataKey, error) {
			return t.secrets.GenerateDataKey(ctx, DataKeyPurpose)
		})
		if err != nil {
			return nil, err
		}
		nonce := make([]byte, nonceSize)
		if _, err := rand.Read(nonce); err != nil {
			return nil, err
		}
		sealed, err := Seal(dk.Plaintext.Reveal(), nonce, dk.Wrapped, t.tenantID, key, plain)
		if err != nil {
			return nil, err
		}
		_, err = retry(ctx, t.cc, "ObjectStore", uri, func() (objectstore.ObjectInfo, error) {
			return t.objects.Put(ctx, key, sealed, objectstore.PutOptions{ContentType: ContentType, IfAbsent: true})
		})
		if err != nil && providererr.CategoryOf(err) != providererr.Conflict { // Conflict：同一载荷别人刚写过，用已有的
			return nil, err
		}
	}
	return referencePayload(uri, digest, int64(len(plain))), nil
}

func (t *TenantCodec) restore(ctx context.Context, p *commonpb.Payload) (*commonpb.Payload, error) {
	ref, err := ParseReference(p)
	if err != nil {
		return nil, err
	}
	m := uriPattern.FindStringSubmatch(ref.URI)
	if m[1] != t.tenantID {
		return nil, providererr.New(providererr.Denied, "claim_check_cross_tenant", fmt.Sprintf("Claim-Check 引用 %s 属于别的租户", ref.URI))
	}
	key := m[2]
	obj, err := retry(ctx, t.cc, "ObjectStore", ref.URI, func() (objectstore.ObjectData, error) { return t.objects.Get(ctx, key) })
	if providererr.CategoryOf(err) == providererr.NotFound {
		return nil, providererr.New(providererr.NotFound, "claim_check_missing", fmt.Sprintf("Claim-Check 对象 %s 不存在（可能已过保留期）", ref.URI))
	}
	if err != nil {
		return nil, err
	}
	header, _, err := ParseSealed(obj.Data)
	if err != nil {
		return nil, withURI(err, ref.URI)
	}
	dataKey, err := retry(ctx, t.cc, "SecretStore", ref.URI, func() ([]byte, error) {
		v, err := t.secrets.UnwrapDataKey(ctx, header.WrappedKey, DataKeyPurpose)
		return v.Reveal(), err
	})
	switch providererr.CategoryOf(err) {
	case providererr.Denied:
		return nil, providererr.New(providererr.Denied, "claim_check_cross_tenant", fmt.Sprintf("Claim-Check 对象 %s 的数据密钥属于别的租户", ref.URI))
	case providererr.Invalid:
		return nil, providererr.New(providererr.Invalid, "claim_check_key_mismatch", fmt.Sprintf("解不开 Claim-Check 对象 %s 的数据密钥：租户的密钥加密密钥不对", ref.URI))
	}
	if err != nil {
		return nil, err
	}
	plain, err := Open(obj.Data, dataKey, t.tenantID, key)
	if err != nil {
		return nil, withURI(err, ref.URI)
	}
	sum := sha256.Sum256(plain)
	if int64(len(plain)) != ref.Size || hex.EncodeToString(sum[:]) != ref.SHA256 {
		return nil, providererr.New(providererr.Invalid, "claim_check_digest_mismatch", fmt.Sprintf("Claim-Check 对象 %s 的内容与引用不符", ref.URI))
	}
	out := &commonpb.Payload{}
	if err := proto.Unmarshal(plain, out); err != nil {
		return nil, providererr.New(providererr.Invalid, "claim_check_digest_mismatch", fmt.Sprintf("Claim-Check 对象 %s 不是 Temporal 载荷", ref.URI))
	}
	return out, nil
}

func withURI(err error, uri string) error {
	var pe *providererr.Error
	if errors.As(err, &pe) {
		return providererr.New(pe.Category, pe.Code, pe.Message+"："+uri)
	}
	return err
}

// retry 在依赖暂时不可用时本地重试几次（指数退避），仍不行报 Transient（claim_check_unavailable）。
func retry[T any](ctx context.Context, cc *ClaimCheck, store, uri string, op func() (T, error)) (T, error) {
	var zero T
	var last error
	for attempt := 0; attempt < cc.opts.Attempts; attempt++ {
		v, err := op()
		if err == nil || !providererr.IsRetryable(err) {
			return v, err
		}
		last = err
		if attempt+1 < cc.opts.Attempts {
			select {
			case <-ctx.Done():
				return zero, ctx.Err()
			case <-time.After(cc.opts.Backoff << attempt):
			}
		}
	}
	code := "unknown"
	var pe *providererr.Error
	if errors.As(last, &pe) {
		code = pe.Code
	}
	return zero, providererr.New(providererr.Transient, "claim_check_unavailable",
		fmt.Sprintf("%s 暂时不可用（%s），已重试 %d 次：%s", store, uri, cc.opts.Attempts, code))
}

// LifecycleRule 给出 claim-check/ 前缀的生命周期规则：ExpireAfter = retention + margin；margin 不大于 0 报 Invalid。
func (c *ClaimCheck) LifecycleRule(retention, margin time.Duration) (objectstore.LifecycleRule, error) {
	if retention <= 0 {
		return objectstore.LifecycleRule{}, providererr.New(providererr.Invalid, "invalid_lifecycle", "Temporal 的 retention 必须大于 0")
	}
	if margin <= 0 {
		return objectstore.LifecycleRule{}, providererr.New(providererr.Invalid, "invalid_lifecycle", "Claim-Check 对象的生命周期必须长于 Temporal 的 retention")
	}
	return objectstore.LifecycleRule{Prefix: KeyPrefix, ExpireAfter: retention + margin}, nil
}

// IsReference 判断载荷是不是 Claim-Check 的引用。
func IsReference(p *commonpb.Payload) bool {
	return string(p.GetMetadata()["encoding"]) == Encoding
}

func badReference(msg string) error {
	return providererr.New(providererr.Invalid, "claim_check_bad_reference", msg)
}

// ParseReference 解析引用；格式不对或版本不支持报 Invalid（claim_check_bad_reference）。
func ParseReference(p *commonpb.Payload) (Reference, error) {
	if v := string(p.GetMetadata()[VersionKey]); v != strconv.Itoa(Version) {
		return Reference{}, badReference("不支持的 Claim-Check 版本：" + v)
	}
	var raw map[string]any
	if err := json.Unmarshal(p.GetData(), &raw); err != nil {
		return Reference{}, badReference("Claim-Check 引用不是合法的 JSON")
	}
	if v, ok := raw["v"].(float64); !ok || v != Version {
		return Reference{}, badReference(fmt.Sprintf("不支持的 Claim-Check 引用版本：%v", raw["v"]))
	}
	uri, _ := raw["uri"].(string)
	if !uriPattern.MatchString(uri) {
		return Reference{}, badReference("Claim-Check 引用的 uri 格式不对")
	}
	digest, _ := raw["sha256"].(string)
	if !sha256Pattern.MatchString(digest) || !strings.HasSuffix(uri, digest) {
		return Reference{}, badReference("Claim-Check 引用的 sha256 不对：" + uri)
	}
	size, ok := raw["size"].(float64)
	if !ok || size < 0 || size != float64(int64(size)) {
		return Reference{}, badReference("Claim-Check 引用的 size 不对：" + uri)
	}
	return Reference{Version: Version, URI: uri, SHA256: digest, Size: int64(size)}, nil
}

func referencePayload(uri, digest string, size int64) *commonpb.Payload {
	data, _ := json.Marshal(map[string]any{"sha256": digest, "size": size, "uri": uri, "v": Version})
	return &commonpb.Payload{
		Metadata: map[string][]byte{"encoding": []byte(Encoding), VersionKey: []byte(strconv.Itoa(Version))},
		Data:     data,
	}
}

// Serialize 是原载荷的确定性序列化（阈值、sha256 与加密都基于它，两种语言字节相同）。
func Serialize(p *commonpb.Payload) ([]byte, error) {
	return proto.MarshalOptions{Deterministic: true}.Marshal(p)
}

func aad(tenantID, objectKey string) []byte {
	return []byte(aadPrefix + tenantID + "\x00" + objectKey)
}

func gcm(key []byte) (cipher.AEAD, error) {
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, providererr.New(providererr.Invalid, "claim_check_decrypt_failed", "Claim-Check 的数据密钥长度不对")
	}
	return cipher.NewGCM(block)
}

// Seal 按格式加密并拼出对象内容（测试向量用固定的密钥与 nonce）。
func Seal(dataKey, nonce, wrappedKey []byte, tenantID, objectKey string, plaintext []byte) ([]byte, error) {
	header, err := json.Marshal(map[string]string{
		"alg":   Algorithm,
		"key":   base64.StdEncoding.EncodeToString(wrappedKey),
		"nonce": base64.StdEncoding.EncodeToString(nonce),
	})
	if err != nil {
		return nil, err
	}
	aead, err := gcm(dataKey)
	if err != nil {
		return nil, err
	}
	out := append([]byte(magic), Version)
	out = binary.BigEndian.AppendUint32(out, uint32(len(header)))
	out = append(out, header...)
	return aead.Seal(out, nonce, plaintext, aad(tenantID, objectKey)), nil
}

// ParseSealed 拆出头部与密文；格式不对报 Invalid（claim_check_decrypt_failed）。
func ParseSealed(data []byte) (SealedHeader, []byte, error) {
	bad := providererr.New(providererr.Invalid, "claim_check_decrypt_failed", "Claim-Check 对象的格式不对")
	if len(data) < 9 || string(data[:4]) != magic {
		return SealedHeader{}, nil, bad
	}
	if data[4] != Version {
		return SealedHeader{}, nil, providererr.New(providererr.Invalid, "claim_check_decrypt_failed", fmt.Sprintf("不支持的 Claim-Check 对象版本：%d", data[4]))
	}
	n := int(binary.BigEndian.Uint32(data[5:9]))
	if n < 0 || len(data) < 9+n {
		return SealedHeader{}, nil, bad
	}
	var h struct {
		Alg   string `json:"alg"`
		Key   string `json:"key"`
		Nonce string `json:"nonce"`
	}
	if err := json.Unmarshal(data[9:9+n], &h); err != nil {
		return SealedHeader{}, nil, bad
	}
	wrapped, err1 := base64.StdEncoding.DecodeString(h.Key)
	nonce, err2 := base64.StdEncoding.DecodeString(h.Nonce)
	if err1 != nil || err2 != nil || h.Alg != Algorithm || len(nonce) != nonceSize {
		return SealedHeader{}, nil, bad
	}
	return SealedHeader{Version: Version, Algorithm: h.Alg, WrappedKey: wrapped, Nonce: nonce}, data[9+n:], nil
}

// Open 解密对象内容；密钥不对或被篡改报 Invalid（claim_check_decrypt_failed）。
func Open(data, dataKey []byte, tenantID, objectKey string) ([]byte, error) {
	header, ciphertext, err := ParseSealed(data)
	if err != nil {
		return nil, err
	}
	aead, err := gcm(dataKey)
	if err != nil {
		return nil, err
	}
	plain, err := aead.Open(nil, header.Nonce, ciphertext, aad(tenantID, objectKey))
	if err != nil {
		return nil, providererr.New(providererr.Invalid, "claim_check_decrypt_failed", "Claim-Check 对象解密失败：数据密钥不对或对象被篡改")
	}
	return plain, nil
}

// --- 接入 Temporal ---

// Resolver 按客户端调用或 activity 的 context 确定租户；确定不了返回 nil。工作流里的载荷按工作流 memo 确定，不经它。
type Resolver interface {
	TenantFromContext(ctx context.Context) (*commonv1.TenantContext, error)
}

type tenantKey struct{}
type workflowIDKey struct{}

// WithTenant 把租户（来自认证身份）放进 context，客户端调用据此编解码。
func WithTenant(ctx context.Context, tenant *commonv1.TenantContext) context.Context {
	return context.WithValue(ctx, tenantKey{}, tenant)
}

// TenantInContext 返回 WithTenant 放进 context 的租户；没有时返回 nil。
func TenantInContext(ctx context.Context) *commonv1.TenantContext {
	if ctx == nil {
		return nil
	}
	t, _ := ctx.Value(tenantKey{}).(*commonv1.TenantContext)
	return t
}

// TenantFromMemo 返回 memo 里的作用域（agentruntime_scope）对应的租户；没有、或是平台作用域时返回 nil。
func TenantFromMemo(memo *commonpb.Memo) *commonv1.TenantContext {
	p := memo.GetFields()[scopeMemoKey]
	if p == nil {
		return nil
	}
	var owner string
	if err := converter.GetDefaultDataConverter().FromPayload(p, &owner); err != nil || owner == "" || owner == platformID {
		return nil
	}
	return &commonv1.TenantContext{TenantId: owner}
}

type memoResolver struct {
	c     client.Client
	cache sync.Map // 根工作流 ID → *commonv1.TenantContext
}

// NewMemoResolver 返回 Worker 用的 Resolver：先看 context 里的租户（WithTenant），再按所属工作流 ID 的根
// （第一个 / 之前）describe 出 memo 的作用域。工作流 ID 取自序列化上下文或 activity 的信息。结果按根 ID 缓存。
func NewMemoResolver(c client.Client) Resolver { return &memoResolver{c: c} }

func (r *memoResolver) TenantFromContext(ctx context.Context) (*commonv1.TenantContext, error) {
	if t := TenantInContext(ctx); t != nil {
		return t, nil
	}
	id, _ := ctx.Value(workflowIDKey{}).(string)
	if id == "" && activity.IsActivity(ctx) {
		id = activity.GetInfo(ctx).WorkflowExecution.ID
	}
	if id == "" || r.c == nil {
		return nil, nil
	}
	root, _, _ := strings.Cut(id, "/")
	if t, ok := r.cache.Load(root); ok {
		return t.(*commonv1.TenantContext), nil
	}
	resp, err := r.c.DescribeWorkflowExecution(ctx, root, "")
	var notFound *serviceerror.NotFound
	if errors.As(err, &notFound) {
		return nil, nil
	}
	if err != nil {
		return nil, providererr.Wrap(providererr.Transient, "claim_check_unavailable", "查不到工作流 "+root+" 的作用域", err)
	}
	t := TenantFromMemo(resp.GetWorkflowExecutionInfo().GetMemo())
	if t != nil {
		r.cache.Store(root, t)
	}
	return t, nil
}

// DataConverter 返回接入了 Claim-Check 的 DataConverter：parent 负责序列化，Claim-Check 作为 PayloadCodec。
// 它实现 workflow.ContextAware：工作流里用 memo 确定租户，其余用 resolver。
func (c *ClaimCheck) DataConverter(parent converter.DataConverter, resolver Resolver) converter.DataConverter {
	return &dataConverter{parent: parent, cc: c, resolver: resolver, ctx: context.Background()}
}

type dataConverter struct {
	parent   converter.DataConverter
	cc       *ClaimCheck
	resolver Resolver
	ctx      context.Context
	// workflowTenant 是工作流 memo 里的租户（WithWorkflowContext）；inWorkflow 表示处在工作流里。
	workflowTenant *commonv1.TenantContext
	inWorkflow     bool
	workflowID     string
}

var (
	_ converter.DataConverter                         = (*dataConverter)(nil)
	_ workflow.ContextAware                           = (*dataConverter)(nil)
	_ converter.DataConverterWithSerializationContext = (*dataConverter)(nil)
)

func (d *dataConverter) WithWorkflowContext(ctx workflow.Context) converter.DataConverter {
	cp := *d
	cp.inWorkflow = true
	cp.workflowTenant = TenantFromMemo(workflow.GetInfo(ctx).Memo)
	return &cp
}

func (d *dataConverter) WithContext(ctx context.Context) converter.DataConverter {
	cp := *d
	cp.ctx = ctx
	return &cp
}

func (d *dataConverter) WithSerializationContext(sc converter.SerializationContext) converter.DataConverter {
	cp := *d
	switch s := sc.(type) {
	case converter.WorkflowSerializationContext:
		cp.workflowID = s.WorkflowID
	case *converter.WorkflowSerializationContext:
		cp.workflowID = s.WorkflowID
	case converter.ActivitySerializationContext:
		cp.workflowID = s.WorkflowID
	case *converter.ActivitySerializationContext:
		cp.workflowID = s.WorkflowID
	}
	return &cp
}

func (d *dataConverter) tenant() (*commonv1.TenantContext, error) {
	if d.workflowTenant != nil {
		return d.workflowTenant, nil
	}
	if d.inWorkflow {
		return nil, nil // 工作流里只认 memo：没有作用域就不卸载
	}
	ctx := d.ctx
	if d.workflowID != "" {
		ctx = context.WithValue(ctx, workflowIDKey{}, d.workflowID)
	}
	return d.resolver.TenantFromContext(ctx)
}

func (d *dataConverter) codec(action string) (*TenantCodec, error) {
	tenant, err := d.tenant()
	if err != nil {
		return nil, err
	}
	if tenant == nil {
		id := d.workflowID
		if id == "" {
			id = "未知"
		}
		return nil, providererr.New(providererr.Denied, "claim_check_no_tenant", fmt.Sprintf("无法确定载荷所属的租户（工作流 %s），拒绝%s", id, action))
	}
	return d.cc.Bind(tenant)
}

func (d *dataConverter) encode(ps []*commonpb.Payload) ([]*commonpb.Payload, error) {
	large := false
	for _, p := range ps {
		b, err := Serialize(p)
		if err != nil {
			return nil, err
		}
		if len(b) > d.cc.opts.Threshold {
			large = true
			break
		}
	}
	if !large {
		return ps, nil
	}
	codec, err := d.codec("卸载")
	if err != nil {
		return nil, err
	}
	return codec.Encode(d.ctx, ps)
}

func (d *dataConverter) decode(ps []*commonpb.Payload) ([]*commonpb.Payload, error) {
	refs := false
	for _, p := range ps {
		if IsReference(p) {
			refs = true
			break
		}
	}
	if !refs {
		return ps, nil
	}
	codec, err := d.codec("取回")
	if err != nil {
		return nil, err
	}
	return codec.Decode(d.ctx, ps)
}

func (d *dataConverter) ToPayload(value any) (*commonpb.Payload, error) {
	p, err := d.parent.ToPayload(value)
	if err != nil || p == nil {
		return p, err
	}
	out, err := d.encode([]*commonpb.Payload{p})
	if err != nil {
		return nil, err
	}
	return out[0], nil
}

func (d *dataConverter) FromPayload(p *commonpb.Payload, valuePtr any) error {
	if p == nil {
		return d.parent.FromPayload(p, valuePtr)
	}
	out, err := d.decode([]*commonpb.Payload{p})
	if err != nil {
		return err
	}
	return d.parent.FromPayload(out[0], valuePtr)
}

func (d *dataConverter) ToPayloads(values ...any) (*commonpb.Payloads, error) {
	ps, err := d.parent.ToPayloads(values...)
	if err != nil || ps == nil {
		return ps, err
	}
	out, err := d.encode(ps.GetPayloads())
	if err != nil {
		return nil, err
	}
	return &commonpb.Payloads{Payloads: out}, nil
}

func (d *dataConverter) FromPayloads(ps *commonpb.Payloads, valuePtrs ...any) error {
	if ps == nil {
		return d.parent.FromPayloads(ps, valuePtrs...)
	}
	out, err := d.decode(ps.GetPayloads())
	if err != nil {
		return err
	}
	return d.parent.FromPayloads(&commonpb.Payloads{Payloads: out}, valuePtrs...)
}

func (d *dataConverter) ToString(p *commonpb.Payload) string {
	if IsReference(p) {
		return "claim-check " + string(p.GetData())
	}
	return d.parent.ToString(p)
}

func (d *dataConverter) ToStrings(ps *commonpb.Payloads) []string {
	out := make([]string, 0, len(ps.GetPayloads()))
	for _, p := range ps.GetPayloads() {
		out = append(out, d.ToString(p))
	}
	return out
}
