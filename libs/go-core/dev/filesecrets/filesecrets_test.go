package filesecrets_test

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/conformance"
	"github.com/agentruntime/agent-runtime/libs/go-core/conformance/secretstoretest"
	"github.com/agentruntime/agent-runtime/libs/go-core/dev/filesecrets"
	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/providers/secretstore"
	"github.com/agentruntime/agent-runtime/libs/go-core/testkit"
	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

const maxTTL = time.Hour

type harness struct {
	provider *filesecrets.Provider
	clock    *testkit.FakeClock
	faults   *testkit.Faults
}

func (h *harness) Provider() secretstore.Provider             { return h.provider }
func (h *harness) Advance(_ context.Context, d time.Duration) { h.clock.Advance(d) }
func (h *harness) InjectFault(op string, c providererr.Category) error {
	h.faults.FailNext(op, c)
	return nil
}
func (h *harness) MaxTTL() time.Duration { return maxTTL }
func (h *harness) SeedSecret(tenantID, name, value string) {
	h.provider.SeedSecret(tenantID, name, value)
}
func (h *harness) GrantRole(tenantID, role string) { h.provider.GrantRole(tenantID, role) }

// 开发实现专有：多个进程共用一份密钥文件（S-06：task-service 与 Worker 互相解开对方的 Claim-Check 数据密钥）。

func TestKEKCreatedByAnotherProcessIsPickedUp(t *testing.T) {
	path := filepath.Join(t.TempDir(), "secrets.json")
	early := conformance.Must(filesecrets.New(filesecrets.Options{Path: path})) // 读文件时还没有 t1 的密钥加密密钥
	late := conformance.Must(filesecrets.New(filesecrets.Options{Path: path}))
	ctx := context.Background()
	t1 := &commonv1.TenantContext{TenantId: "t1"}
	key := conformance.Must(conformance.Must(late.Bind(t1)).GenerateDataKey(ctx, "claim_check"))
	got, err := conformance.Must(early.Bind(t1)).UnwrapDataKey(ctx, key.Wrapped, "claim_check")
	if err != nil || !got.Equal(key.Plaintext) {
		t.Fatalf("缺密钥加密密钥时应重新读文件：%v", err)
	}
}

func TestConcurrentProcessesNeverOverwriteEachOthersKEK(t *testing.T) {
	path := filepath.Join(t.TempDir(), "secrets.json")
	a := conformance.Must(filesecrets.New(filesecrets.Options{Path: path}))
	b := conformance.Must(filesecrets.New(filesecrets.Options{Path: path}))
	ctx := context.Background()
	t1 := &commonv1.TenantContext{TenantId: "t1"}
	keyA := conformance.Must(conformance.Must(a.Bind(t1)).GenerateDataKey(ctx, "claim_check"))
	keyB := conformance.Must(conformance.Must(b.Bind(t1)).GenerateDataKey(ctx, "claim_check")) // b 生成前重新读文件，沿用 a 的
	fresh := conformance.Must(filesecrets.New(filesecrets.Options{Path: path}))
	for name, key := range map[string]secretstore.DataKey{"a": keyA, "b": keyB} {
		got, err := conformance.Must(fresh.Bind(t1)).UnwrapDataKey(ctx, key.Wrapped, "claim_check")
		if err != nil || !got.Equal(key.Plaintext) {
			t.Errorf("%s 生成的数据密钥应能用文件里的密钥加密密钥解开：%v", name, err)
		}
	}
}

func TestConformance(t *testing.T) {
	secretstoretest.Run(t, func(t *testing.T) secretstoretest.Harness {
		h := &harness{clock: testkit.NewFakeClock(), faults: testkit.NewFaults()}
		h.provider = conformance.Must(filesecrets.New(filesecrets.Options{
			Path: filepath.Join(t.TempDir(), "secrets.json"), MaxTTL: maxTTL, Clock: h.clock, Faults: h.faults,
		}))
		t.Cleanup(func() { _ = h.provider.Close() })
		return h
	})
}
