package config

import (
	"testing"
	"time"
)

func env(m map[string]string) func(string) string {
	base := map[string]string{"AR_SANDBOX_BACKENDS_FILE": "/b.json", "AR_SECRETS_FILE": "/s.json"}
	for k, v := range m {
		base[k] = v
	}
	return func(k string) string { return base[k] }
}

// 搬进服务的部署配置（原来在 Worker：AR_SANDBOX_HOLD_MAX_S、AR_WORKSPACE_COLD_AFTER_DAYS、AR_SANDBOX_RECONCILE_INTERVAL_S）。
func TestDeploymentSettingsMovedFromTheWorker(t *testing.T) {
	c, err := FromEnv(env(nil))
	if err != nil {
		t.Fatal(err)
	}
	if c.HoldMax != 2*time.Hour || c.ColdDays != 0 || c.ReconcileEvery != time.Minute || c.PolicyTTL != time.Minute {
		t.Fatalf("默认值：%+v", c)
	}
	if c.ChunkedSnapshots {
		t.Fatal("new archive writes must remain disabled until a compatible rollback release exists")
	}
	enabled, err := FromEnv(env(map[string]string{"AR_WORKSPACE_CHUNKED_SNAPSHOTS": "true"}))
	if err != nil || !enabled.ChunkedSnapshots {
		t.Fatalf("enable chunk writes: %+v %v", enabled, err)
	}
	c, err = FromEnv(env(map[string]string{"AR_SANDBOX_HOLD_MAX_S": "600", "AR_WORKSPACE_COLD_AFTER_DAYS": "14", "AR_SANDBOX_POLICY_TTL_S": "0"}))
	if err != nil || c.HoldMax != 10*time.Minute || c.ColdDays != 14 || c.PolicyTTL >= 0 {
		t.Fatalf("%+v %v", c, err)
	}
	for _, bad := range []map[string]string{
		{"AR_SANDBOX_HOLD_MAX_S": "-1"}, {"AR_WORKSPACE_COLD_AFTER_DAYS": "-1"}, {"AR_WORKSPACE_COLD_AFTER_DAYS": "abc"},
		{"AR_WORKSPACE_COLD_AFTER_DAYS": "366"},
		{"AR_WORKSPACE_CHUNKED_SNAPSHOTS": "yes"},
	} {
		if _, err := FromEnv(env(bad)); err == nil {
			t.Errorf("%v 要拒绝", bad)
		}
	}
}

func TestRequiredSettingsAndProductionProfile(t *testing.T) {
	if _, err := FromEnv(func(string) string { return "" }); err == nil {
		t.Fatal("没有后端登记时拒绝启动")
	}
	if _, err := FromEnv(env(map[string]string{"AR_PROFILE": "production"})); err == nil {
		t.Fatal("生产模式没有 AR_RELATIONAL_DSN 拒绝启动")
	}
}
