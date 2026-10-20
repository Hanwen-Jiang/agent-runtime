package claimcheck_test

// S-06 验收：Claim-Check 编解码（Go 侧；设计书 4.7、附录 K.3、L.8；计划 S-06）。与 Python 侧的
// libs/py-core/tests/adapters/test_claim_check.py 一一对应：阈值上下的载荷各一组；格式按共用的测试向量；
// 密钥错误、对象被篡改、对象不存在时报错明确；租户 A 的引用不能用租户 B 的上下文解码；对象在租户前缀下；
// 生命周期长于 retention；ObjectStore 暂时不可用时本地重试，仍不行报 transient；接入 Temporal 的 DataConverter
// 在工作流里按 memo 确定租户、在客户端调用里按 context 里的租户。跨语言往返在 tests/contract。

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	commonpb "go.temporal.io/api/common/v1"
	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/converter"
	"go.temporal.io/sdk/testsuite"
	"go.temporal.io/sdk/workflow"
	"google.golang.org/protobuf/proto"

	temporaladapter "github.com/agentruntime/agent-runtime/libs/go-core/adapters/temporal"
	"github.com/agentruntime/agent-runtime/libs/go-core/adapters/temporal/claimcheck"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/diskobjects"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/filesecrets"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/objectstore"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

var (
	t1 = &commonv1.TenantContext{TenantId: "t1"}
	t2 = &commonv1.TenantContext{TenantId: "t2"}
)

const vectorPath = "../../../../../tests/contract/fixtures/claimcheck/vector.json"

type fixture struct {
	t            *testing.T
	dir          string
	objectFaults *testkit.Faults
	secretFaults *testkit.Faults
	objects      *diskobjects.Provider
	secrets      *filesecrets.Provider
	cc           *claimcheck.ClaimCheck
}

func newFixture(t *testing.T) *fixture {
	t.Helper()
	dir := t.TempDir()
	f := &fixture{t: t, dir: dir, objectFaults: testkit.NewFaults(), secretFaults: testkit.NewFaults()}
	var err error
	f.objects, err = diskobjects.New(diskobjects.Options{Root: filepath.Join(dir, "objects"), SigningKey: []byte("claim-check-test"), Faults: f.objectFaults})
	if err != nil {
		t.Fatal(err)
	}
	f.secrets, err = filesecrets.New(filesecrets.Options{Path: filepath.Join(dir, "secrets.json"), Faults: f.secretFaults})
	if err != nil {
		t.Fatal(err)
	}
	f.cc = f.withThreshold(0)
	return f
}

func (f *fixture) withThreshold(threshold int) *claimcheck.ClaimCheck {
	f.t.Helper()
	cc, err := claimcheck.New(claimcheck.Options{Objects: f.objects, Secrets: f.secrets, Threshold: threshold, Backoff: time.Millisecond})
	if err != nil {
		f.t.Fatal(err)
	}
	return cc
}

func (f *fixture) codec(cc *claimcheck.ClaimCheck, tenant *commonv1.TenantContext) *claimcheck.TenantCodec {
	f.t.Helper()
	c, err := cc.Bind(tenant)
	if err != nil {
		f.t.Fatal(err)
	}
	return c
}

func (f *fixture) encode(tenant *commonv1.TenantContext, ps ...*commonpb.Payload) []*commonpb.Payload {
	f.t.Helper()
	out, err := f.codec(f.cc, tenant).Encode(context.Background(), ps)
	if err != nil {
		f.t.Fatal(err)
	}
	return out
}

func (f *fixture) storedKeys(tenant *commonv1.TenantContext) []string {
	f.t.Helper()
	s, err := f.objects.Bind(tenant)
	if err != nil {
		f.t.Fatal(err)
	}
	page, err := s.List(context.Background(), objectstore.ListOptions{Prefix: claimcheck.KeyPrefix})
	if err != nil {
		f.t.Fatal(err)
	}
	var keys []string
	for _, it := range page.Items {
		keys = append(keys, it.Key)
	}
	return keys
}

func sizeOf(t *testing.T, p *commonpb.Payload) int {
	t.Helper()
	b, err := proto.MarshalOptions{Deterministic: true}.Marshal(p)
	if err != nil {
		t.Fatal(err)
	}
	return len(b)
}

// payloadOfSize 返回确定性序列化后恰好 size 字节的载荷。
func payloadOfSize(t *testing.T, size int, fill byte) *commonpb.Payload {
	t.Helper()
	n := size - 30
	if n < 0 {
		n = 0
	}
	for range 10 {
		p := &commonpb.Payload{Metadata: map[string][]byte{"encoding": []byte("json/plain")}, Data: bytes.Repeat([]byte{fill}, n)}
		got := sizeOf(t, p)
		if got == size {
			return p
		}
		n += size - got
	}
	t.Fatalf("凑不出 %d 字节的载荷", size)
	return nil
}

func wantCode(t *testing.T, err error, category providererr.Category, code string) {
	t.Helper()
	var pe *providererr.Error
	if !errors.As(err, &pe) || pe.Category != category || pe.Code != code {
		t.Fatalf("应报 %v / %s，实际 %v", category, code, err)
	}
}

// --- 阈值上下 ---

func TestPayloadsUpToTheThresholdPassThrough(t *testing.T) {
	for _, size := range []int{64, claimcheck.DefaultThreshold - 1, claimcheck.DefaultThreshold} {
		f := newFixture(t)
		p := payloadOfSize(t, size, 'x')
		out := f.encode(t1, p)
		if len(out) != 1 || !proto.Equal(out[0], p) || claimcheck.IsReference(out[0]) {
			t.Errorf("%d 字节的载荷应原样保留", size)
		}
		if keys := f.storedKeys(t1); len(keys) != 0 {
			t.Errorf("不超过阈值不该写对象，实际 %v", keys)
		}
	}
}

func TestPayloadsAboveTheThresholdAreOffloadedAndRestored(t *testing.T) {
	for _, size := range []int{claimcheck.DefaultThreshold + 1, 1 << 20} {
		f := newFixture(t)
		p := payloadOfSize(t, size, 'x')
		ref := f.encode(t1, p)[0]
		if !claimcheck.IsReference(ref) || string(ref.GetMetadata()["encoding"]) != claimcheck.Encoding || string(ref.GetMetadata()[claimcheck.VersionKey]) != "1" {
			t.Fatalf("应换成引用，实际 metadata %v", ref.GetMetadata())
		}
		if sizeOf(t, ref) >= 1024 {
			t.Errorf("历史里只留引用，实际 %d 字节", sizeOf(t, ref))
		}
		plain, err := claimcheck.Serialize(p)
		if err != nil {
			t.Fatal(err)
		}
		sum := sha256.Sum256(plain)
		digest := hex.EncodeToString(sum[:])
		got, err := claimcheck.ParseReference(ref)
		want := claimcheck.Reference{Version: 1, URI: "obj://t1/" + claimcheck.KeyPrefix + digest, SHA256: digest, Size: int64(size)}
		if err != nil || got != want {
			t.Fatalf("引用应为 %+v，实际 %+v（%v）", want, got, err)
		}
		if keys := f.storedKeys(t1); len(keys) != 1 || keys[0] != claimcheck.KeyPrefix+digest {
			t.Errorf("对象应在 t1 的 %s<sha256> 下，实际 %v", claimcheck.KeyPrefix, keys)
		}
		s, _ := f.objects.Bind(t1)
		obj, err := s.Get(context.Background(), claimcheck.KeyPrefix+digest)
		if err != nil || bytes.Contains(obj.Data, bytes.Repeat([]byte{'x'}, 64)) {
			t.Errorf("对象里应是密文（%v）", err)
		}
		restored, err := f.codec(f.cc, t1).Decode(context.Background(), []*commonpb.Payload{ref})
		if err != nil || !proto.Equal(restored[0], p) {
			t.Fatalf("解码应还原出原载荷（%v）", err)
		}
	}
}

func TestThresholdIsConfigurable(t *testing.T) {
	f := newFixture(t)
	out, err := f.codec(f.withThreshold(1024), t1).Encode(context.Background(), []*commonpb.Payload{payloadOfSize(t, 1024, 'x'), payloadOfSize(t, 1025, 'y')})
	if err != nil {
		t.Fatal(err)
	}
	if claimcheck.IsReference(out[0]) || !claimcheck.IsReference(out[1]) {
		t.Error("阈值 1024：1024 字节保留，1025 字节卸载")
	}
}

func TestBatchKeepsOrderAndOnlyLargePayloadsChange(t *testing.T) {
	f := newFixture(t)
	batch := []*commonpb.Payload{payloadOfSize(t, 100, 'a'), payloadOfSize(t, claimcheck.DefaultThreshold+10, 'b'), payloadOfSize(t, 200, 'c'), payloadOfSize(t, 2*claimcheck.DefaultThreshold, 'd')}
	out := f.encode(t1, batch...)
	for i, want := range []bool{false, true, false, true} {
		if claimcheck.IsReference(out[i]) != want {
			t.Errorf("第 %d 个载荷：是否引用应为 %v", i, want)
		}
	}
	back, err := f.codec(f.cc, t1).Decode(context.Background(), out)
	if err != nil {
		t.Fatal(err)
	}
	for i := range batch {
		if !proto.Equal(back[i], batch[i]) {
			t.Errorf("第 %d 个载荷没有还原", i)
		}
	}
}

func TestReencodingReusesTheStoredObject(t *testing.T) {
	f := newFixture(t)
	p := payloadOfSize(t, 2*claimcheck.DefaultThreshold, 'x')
	first, second := f.encode(t1, p)[0], f.encode(t1, p)[0]
	if !proto.Equal(first, second) {
		t.Error("同一载荷再编码（例如重放）得到同一个引用")
	}
	if keys := f.storedKeys(t1); len(keys) != 1 {
		t.Errorf("只存一个对象，实际 %v", keys)
	}
}

// --- 跨语言一致的格式（测试向量）---

type vector struct {
	Spec struct {
		ReferenceEncoding   string `json:"reference_encoding"`
		ReferenceVersionKey string `json:"reference_version_key"`
		ReferenceVersion    int    `json:"reference_version"`
		ObjectKeyPrefix     string `json:"object_key_prefix"`
		DataKeyPurpose      string `json:"data_key_purpose"`
		Algorithm           string `json:"algorithm"`
	} `json:"spec"`
	Inputs struct {
		TenantID      string `json:"tenant_id"`
		DataKeyHex    string `json:"data_key_hex"`
		NonceHex      string `json:"nonce_hex"`
		WrappedKeyHex string `json:"wrapped_key_hex"`
		Payload       struct {
			Metadata map[string]string `json:"metadata"`
			DataUTF8 string            `json:"data_utf8"`
		} `json:"payload"`
		PlaintextHex string `json:"plaintext_hex"`
	} `json:"inputs"`
	Expected struct {
		PlaintextSHA256   string            `json:"plaintext_sha256"`
		ObjectKey         string            `json:"object_key"`
		SealedHex         string            `json:"sealed_hex"`
		ReferenceMetadata map[string]string `json:"reference_metadata"`
		ReferenceData     string            `json:"reference_data"`
	} `json:"expected"`
}

func loadVector(t *testing.T) (vector, *commonpb.Payload) {
	t.Helper()
	raw, err := os.ReadFile(vectorPath)
	if err != nil {
		t.Fatal(err)
	}
	var v vector
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatal(err)
	}
	p := &commonpb.Payload{Metadata: map[string][]byte{}, Data: []byte(v.Inputs.Payload.DataUTF8)}
	for k, val := range v.Inputs.Payload.Metadata {
		p.Metadata[k] = []byte(val)
	}
	return v, p
}

func unhex(t *testing.T, s string) []byte {
	t.Helper()
	b, err := hex.DecodeString(s)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func TestFormatFollowsTheSharedVector(t *testing.T) {
	v, p := loadVector(t)
	if v.Spec.ReferenceEncoding != claimcheck.Encoding || v.Spec.ReferenceVersionKey != claimcheck.VersionKey || v.Spec.ReferenceVersion != claimcheck.Version ||
		v.Spec.ObjectKeyPrefix != claimcheck.KeyPrefix || v.Spec.DataKeyPurpose != claimcheck.DataKeyPurpose || v.Spec.Algorithm != claimcheck.Algorithm {
		t.Fatalf("常量与测试向量不一致：%+v", v.Spec)
	}
	plain, err := claimcheck.Serialize(p)
	if err != nil || hex.EncodeToString(plain) != v.Inputs.PlaintextHex {
		t.Fatalf("确定性序列化应与 Python 侧字节相同，实际 %x（%v）", plain, err)
	}
	sealed, err := claimcheck.Seal(unhex(t, v.Inputs.DataKeyHex), unhex(t, v.Inputs.NonceHex), unhex(t, v.Inputs.WrappedKeyHex), v.Inputs.TenantID, v.Expected.ObjectKey, plain)
	if err != nil || hex.EncodeToString(sealed) != v.Expected.SealedHex {
		t.Fatalf("对象内容应与测试向量相同（%v）", err)
	}
	header, _, err := claimcheck.ParseSealed(sealed)
	if err != nil || header.Version != 1 || header.Algorithm != claimcheck.Algorithm ||
		!bytes.Equal(header.WrappedKey, unhex(t, v.Inputs.WrappedKeyHex)) || !bytes.Equal(header.Nonce, unhex(t, v.Inputs.NonceHex)) {
		t.Errorf("头部不对：%+v（%v）", header, err)
	}
	opened, err := claimcheck.Open(sealed, unhex(t, v.Inputs.DataKeyHex), v.Inputs.TenantID, v.Expected.ObjectKey)
	if err != nil || !bytes.Equal(opened, plain) {
		t.Errorf("Open 应还原明文（%v）", err)
	}
}

func TestReferenceBytesFollowTheSharedVector(t *testing.T) {
	v, p := loadVector(t)
	f := newFixture(t)
	ref, err := f.codec(f.withThreshold(1), &commonv1.TenantContext{TenantId: v.Inputs.TenantID}).Encode(context.Background(), []*commonpb.Payload{p})
	if err != nil {
		t.Fatal(err)
	}
	got := map[string]string{}
	for k, val := range ref[0].GetMetadata() {
		got[k] = string(val)
	}
	if len(got) != len(v.Expected.ReferenceMetadata) {
		t.Errorf("引用的 metadata 应为 %v，实际 %v", v.Expected.ReferenceMetadata, got)
	}
	for k, val := range v.Expected.ReferenceMetadata {
		if got[k] != val {
			t.Errorf("metadata[%s] 应为 %q，实际 %q", k, val, got[k])
		}
	}
	if string(ref[0].GetData()) != v.Expected.ReferenceData {
		t.Errorf("引用的 JSON 应与 Python 侧逐字节相同：\n应为 %s\n实际 %s", v.Expected.ReferenceData, ref[0].GetData())
	}
}

func TestOpenRejectsTamperingAndRebinding(t *testing.T) {
	v, _ := loadVector(t)
	sealed, key := unhex(t, v.Expected.SealedHex), unhex(t, v.Inputs.DataKeyHex)
	tampered := [][]byte{
		append(append([]byte{}, sealed[:len(sealed)-1]...), sealed[len(sealed)-1]^1),
		append(append(append([]byte{}, sealed[:4]...), 2), sealed[5:]...),
		append([]byte("XXXX"), sealed[4:]...),
	}
	for _, data := range tampered {
		_, err := claimcheck.Open(data, key, "t1", v.Expected.ObjectKey)
		wantCode(t, err, providererr.Invalid, "claim_check_decrypt_failed")
	}
	// AAD 带租户与对象键：挪到别的租户或别的键下都解不开。
	if _, err := claimcheck.Open(sealed, key, "t2", v.Expected.ObjectKey); err == nil {
		t.Error("换了租户不该解得开")
	}
	if _, err := claimcheck.Open(sealed, key, "t1", claimcheck.KeyPrefix+strings.Repeat("0", 64)); err == nil {
		t.Error("换了对象键不该解得开")
	}
}

// --- 报错明确 ---

func TestWrongKeyFailsWithAClearError(t *testing.T) {
	f := newFixture(t)
	ref := f.encode(t1, payloadOfSize(t, 2*claimcheck.DefaultThreshold, 'x'))[0]
	other, err := filesecrets.New(filesecrets.Options{Path: filepath.Join(f.dir, "other-secrets.json")})
	if err != nil {
		t.Fatal(err)
	}
	wrong, err := claimcheck.New(claimcheck.Options{Objects: f.objects, Secrets: other})
	if err != nil {
		t.Fatal(err)
	}
	_, err = f.codec(wrong, t1).Decode(context.Background(), []*commonpb.Payload{ref})
	wantCode(t, err, providererr.Invalid, "claim_check_key_mismatch")
	parsed, _ := claimcheck.ParseReference(ref)
	if !strings.Contains(err.Error(), parsed.URI) {
		t.Errorf("报错应带出引用的 uri：%v", err)
	}
}

func TestTamperedObjectFailsToDecrypt(t *testing.T) {
	f := newFixture(t)
	ref := f.encode(t1, payloadOfSize(t, 2*claimcheck.DefaultThreshold, 'x'))[0]
	parsed, _ := claimcheck.ParseReference(ref)
	s, _ := f.objects.Bind(t1)
	key, _ := s.KeyOf(parsed.URI)
	obj, err := s.Get(context.Background(), key)
	if err != nil {
		t.Fatal(err)
	}
	obj.Data[len(obj.Data)-1] ^= 1
	if _, err := s.Put(context.Background(), key, obj.Data, objectstore.PutOptions{ContentType: claimcheck.ContentType}); err != nil {
		t.Fatal(err)
	}
	_, err = f.codec(f.cc, t1).Decode(context.Background(), []*commonpb.Payload{ref})
	wantCode(t, err, providererr.Invalid, "claim_check_decrypt_failed")
	if !strings.Contains(err.Error(), parsed.URI) {
		t.Errorf("报错应带出引用的 uri：%v", err)
	}
}

func TestMissingObjectIsReportedAsNotFound(t *testing.T) {
	f := newFixture(t)
	ref := f.encode(t1, payloadOfSize(t, 2*claimcheck.DefaultThreshold, 'x'))[0]
	parsed, _ := claimcheck.ParseReference(ref)
	s, _ := f.objects.Bind(t1)
	key, _ := s.KeyOf(parsed.URI)
	if _, err := s.Delete(context.Background(), key); err != nil {
		t.Fatal(err)
	}
	_, err := f.codec(f.cc, t1).Decode(context.Background(), []*commonpb.Payload{ref})
	wantCode(t, err, providererr.NotFound, "claim_check_missing")
}

func TestBadOrUnknownReferencesAreRejected(t *testing.T) {
	f := newFixture(t)
	for _, data := range []string{`not json`, `{"sha256":"ab","size":1,"uri":"obj://t1/claim-check/ab","v":2}`, `{"uri":"obj://t1/claim-check/ab","v":1}`} {
		ref := &commonpb.Payload{Metadata: map[string][]byte{"encoding": []byte(claimcheck.Encoding), claimcheck.VersionKey: []byte("1")}, Data: []byte(data)}
		_, err := f.codec(f.cc, t1).Decode(context.Background(), []*commonpb.Payload{ref})
		wantCode(t, err, providererr.Invalid, "claim_check_bad_reference")
	}
}

// --- 租户隔离 ---

func TestReferenceFromAnotherTenantCannotBeDecoded(t *testing.T) {
	f := newFixture(t)
	ref := f.encode(t1, payloadOfSize(t, 2*claimcheck.DefaultThreshold, 'x'))[0]
	_, err := f.codec(f.cc, t2).Decode(context.Background(), []*commonpb.Payload{ref})
	wantCode(t, err, providererr.Denied, "claim_check_cross_tenant")
}

func TestObjectsLiveUnderTheTenantPrefixWithTenantDataKeys(t *testing.T) {
	f := newFixture(t)
	ref := f.encode(t1, payloadOfSize(t, 2*claimcheck.DefaultThreshold, 'x'))[0]
	parsed, _ := claimcheck.ParseReference(ref)
	if !strings.HasPrefix(parsed.URI, "obj://t1/claim-check/") {
		t.Errorf("引用应指向 t1 的前缀，实际 %s", parsed.URI)
	}
	if keys := f.storedKeys(t2); len(keys) != 0 {
		t.Errorf("t2 的作用域里不该有对象，实际 %v", keys)
	}
	s2, _ := f.objects.Bind(t2)
	if _, err := s2.KeyOf(parsed.URI); providererr.CategoryOf(err) != providererr.Denied {
		t.Errorf("t2 解析 t1 的引用应报 Denied，实际 %v", err)
	}
	s1, _ := f.objects.Bind(t1)
	key, _ := s1.KeyOf(parsed.URI)
	obj, err := s1.Get(context.Background(), key)
	if err != nil {
		t.Fatal(err)
	}
	header, _, err := claimcheck.ParseSealed(obj.Data)
	if err != nil {
		t.Fatal(err)
	}
	sec2, _ := f.secrets.Bind(t2)
	if _, err := sec2.UnwrapDataKey(context.Background(), header.WrappedKey, claimcheck.DataKeyPurpose); providererr.CategoryOf(err) != providererr.Denied {
		t.Errorf("t2 解不开 t1 的数据密钥，实际 %v", err)
	}
}

// --- ObjectStore 暂时不可用 ---

func TestTransientObjectStoreErrorsAreRetried(t *testing.T) {
	f := newFixture(t)
	p := payloadOfSize(t, 2*claimcheck.DefaultThreshold, 'x')
	f.objectFaults.FailNext("Put", providererr.Transient)
	f.objectFaults.FailNext("Put", providererr.Transient)
	ref := f.encode(t1, p)[0]
	f.objectFaults.FailNext("Get", providererr.Transient)
	f.objectFaults.FailNext("Get", providererr.Transient)
	back, err := f.codec(f.cc, t1).Decode(context.Background(), []*commonpb.Payload{ref})
	if err != nil || !proto.Equal(back[0], p) {
		t.Fatalf("重试后应成功（%v）", err)
	}
}

func TestPersistentOutageIsReportedAsUnavailable(t *testing.T) {
	f := newFixture(t)
	for range 10 {
		f.objectFaults.FailNext("Put", providererr.Transient)
	}
	_, err := f.codec(f.cc, t1).Encode(context.Background(), []*commonpb.Payload{payloadOfSize(t, 2*claimcheck.DefaultThreshold, 'x')})
	wantCode(t, err, providererr.Transient, "claim_check_unavailable")
	if !strings.Contains(err.Error(), "ObjectStore") {
		t.Errorf("报错应说明是 ObjectStore 不可用：%v", err)
	}
}

// --- 生命周期 ---

func TestLifecycleRuleOutlivesTheTemporalRetention(t *testing.T) {
	f := newFixture(t)
	rule, err := f.cc.LifecycleRule(30*24*time.Hour, claimcheck.DefaultLifecycleMargin)
	if err != nil || rule.Prefix != claimcheck.KeyPrefix || rule.ExpireAfter != 30*24*time.Hour+claimcheck.DefaultLifecycleMargin {
		t.Errorf("规则不对：%+v（%v）", rule, err)
	}
	if _, err := f.cc.LifecycleRule(30*24*time.Hour, 0); providererr.CategoryOf(err) != providererr.Invalid {
		t.Errorf("margin 为 0 应报 Invalid，实际 %v", err)
	}
}

// --- 接入 Temporal 的 DataConverter ---

type stubResolver struct{ tenant *commonv1.TenantContext }

func (r stubResolver) TenantFromContext(ctx context.Context) (*commonv1.TenantContext, error) {
	return r.tenant, nil
}

func TestTenantFromMemo(t *testing.T) {
	memo := &commonpb.Memo{Fields: map[string]*commonpb.Payload{}}
	p, err := converter.GetDefaultDataConverter().ToPayload("t1")
	if err != nil {
		t.Fatal(err)
	}
	memo.Fields[temporaladapter.ScopeMemoKey] = p
	if got := claimcheck.TenantFromMemo(memo); got.GetTenantId() != "t1" {
		t.Errorf("memo 的作用域应解析成租户 t1，实际 %v", got)
	}
	if claimcheck.TenantFromMemo(&commonpb.Memo{}) != nil || claimcheck.TenantFromMemo(nil) != nil {
		t.Error("没有作用域时返回 nil")
	}
	platform, _ := converter.GetDefaultDataConverter().ToPayload("_platform")
	if claimcheck.TenantFromMemo(&commonpb.Memo{Fields: map[string]*commonpb.Payload{temporaladapter.ScopeMemoKey: platform}}) != nil {
		t.Error("平台作用域不对应租户")
	}
}

func TestClientCallsUseTheTenantInTheContext(t *testing.T) {
	f := newFixture(t)
	dc := f.cc.DataConverter(converter.GetDefaultDataConverter(), claimcheck.NewMemoResolver(nil))
	big := strings.Repeat("大", claimcheck.DefaultThreshold/2)
	aware, ok := dc.(workflow.ContextAware)
	if !ok {
		t.Fatal("DataConverter 应实现 workflow.ContextAware")
	}
	ctxDC := aware.WithContext(claimcheck.WithTenant(context.Background(), t1))
	p, err := ctxDC.ToPayload(big)
	if err != nil || !claimcheck.IsReference(p) {
		t.Fatalf("大载荷应换成引用（%v）", err)
	}
	var back string
	if err := ctxDC.FromPayload(p, &back); err != nil || back != big {
		t.Fatalf("应解码回原值（%v）", err)
	}
	if _, err := aware.WithContext(context.Background()).ToPayload(big); providererr.CategoryOf(err) != providererr.Denied {
		t.Errorf("确定不了租户时拒绝卸载，实际 %v", err)
	}
	small, err := aware.WithContext(context.Background()).ToPayload("小")
	if err != nil || claimcheck.IsReference(small) {
		t.Errorf("小载荷不需要租户，原样编码（%v）", err)
	}
}

func bigEchoWorkflow(ctx workflow.Context, in string) (string, error) {
	ctx = workflow.WithActivityOptions(ctx, workflow.ActivityOptions{StartToCloseTimeout: time.Minute})
	var out string
	err := workflow.ExecuteActivity(ctx, "echo", in).Get(ctx, &out)
	return out, err
}

func TestWorkflowPayloadsUseTheTenantInTheWorkflowMemo(t *testing.T) {
	f := newFixture(t)
	var suite testsuite.WorkflowTestSuite
	env := suite.NewTestWorkflowEnvironment()
	env.SetDataConverter(f.cc.DataConverter(converter.GetDefaultDataConverter(), stubResolver{tenant: t1}))
	env.SetMemoOnStart(map[string]any{temporaladapter.ScopeMemoKey: "t1"})
	env.RegisterWorkflow(bigEchoWorkflow)
	env.RegisterActivityWithOptions(func(ctx context.Context, s string) (string, error) { return s, nil }, activity.RegisterOptions{Name: "echo"})
	big := strings.Repeat("大", claimcheck.DefaultThreshold/2)
	env.ExecuteWorkflow(bigEchoWorkflow, big)
	if !env.IsWorkflowCompleted() || env.GetWorkflowError() != nil {
		t.Fatalf("工作流应完成：%v", env.GetWorkflowError())
	}
	var out string
	if err := env.GetWorkflowResult(&out); err != nil || out != big {
		t.Fatalf("大载荷经工作流与 activity 往返不变（%v）", err)
	}
	if keys := f.storedKeys(t1); len(keys) == 0 {
		t.Error("工作流里卸载的载荷应存进 memo 所示租户 t1 的作用域")
	}
}
