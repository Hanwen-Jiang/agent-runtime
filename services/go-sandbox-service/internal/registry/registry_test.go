package registry

// 后端登记与租户策略的解析（SBX-01 从 Worker 的 tests/test_sandbox_shell.py、test_sandbox_backend_registry.py（D-19）与
// test_sandbox_quota.py 搬来）。
//
//   - 登记文件的格式不对时加载就失败：default 没登记、kind 与 isolation 不认识、后端名格式不对、没有 backends。
//   - D-19：default 指向 enabled: false 的后端时加载失败；capabilities 不认识、firecracker 没配 workspace: archive、enabled 不是
//     布尔值、firecracker 没写 manager_url 都失败，原因里有后端名；登记的能力查得到。
//   - 租户策略：数字从策略里读，空闲与存活时长只能调低部署的默认值；没有策略时用部署的默认后端。

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"
)

func parse(t *testing.T, doc map[string]any) (*Registry, error) {
	t.Helper()
	raw, _ := json.Marshal(doc)
	return Parse(raw, Options{WorkspaceRoot: t.TempDir(), StateDir: t.TempDir()})
}

var firecrackerOff = map[string]any{"kind": "firecracker", "isolation": "microvm", "workspace": "archive", "enabled": false,
	"manager_url": "http://10.42.0.1:8097", "capabilities": []any{"snapshot", "block_attach", "vsock"}}

func TestBackendsFileIsValidated(t *testing.T) {
	reg, err := parse(t, map[string]any{"default": "a", "backends": map[string]any{"a": map[string]any{"kind": "local"}}})
	if err != nil || strings.Join(reg.Names(), ",") != "a" {
		t.Fatalf("合法的登记：%v %v", reg, err)
	}
	for _, tc := range []struct {
		doc  map[string]any
		what string
	}{
		{map[string]any{"default": "b", "backends": map[string]any{"a": map[string]any{"kind": "local"}}}, "default"},
		{map[string]any{"default": "a", "backends": map[string]any{"a": map[string]any{"kind": "vm"}}}, "kind"},
		{map[string]any{"default": "a", "backends": map[string]any{"a": map[string]any{"kind": "local", "isolation": "strong"}}}, "isolation"},
		{map[string]any{"default": "A", "backends": map[string]any{"A": map[string]any{"kind": "local"}}}, "格式"},
		{map[string]any{"default": "a"}, "backends"},
	} {
		if _, err := parse(t, tc.doc); err == nil || !strings.Contains(err.Error(), tc.what) {
			t.Errorf("%v：要因为 %s 失败，得到 %v", tc.doc, tc.what, err)
		}
	}
}

func TestDefaultPointingAtADisabledBackendFailsToLoad(t *testing.T) {
	_, err := parse(t, map[string]any{"default": "gvisor", "backends": map[string]any{
		"gvisor": map[string]any{"kind": "local", "isolation": "container", "enabled": false}}})
	if err == nil || !strings.Contains(err.Error(), "gvisor") {
		t.Fatalf("err = %v", err)
	}
}

func TestBadRegistryEntriesFailToLoad(t *testing.T) {
	with := func(base map[string]any, k string, v any) map[string]any {
		out := map[string]any{}
		for kk, vv := range base {
			out[kk] = vv
		}
		out[k] = v
		return out
	}
	for _, tc := range []struct {
		backend map[string]any
		why     string
	}{
		{map[string]any{"kind": "local", "isolation": "container", "capabilities": []any{"teleport"}}, "capabilities"},
		{map[string]any{"kind": "local", "isolation": "container", "capabilities": "snapshot"}, "capabilities"},
		{map[string]any{"kind": "local", "isolation": "container", "enabled": "no"}, "enabled"},
		{with(firecrackerOff, "workspace", "mount"), "archive"},
		{with(firecrackerOff, "manager_url", ""), "manager_url"},
	} {
		_, err := parse(t, map[string]any{"default": "local", "backends": map[string]any{
			"local": map[string]any{"kind": "local", "isolation": "none"}, "vm": tc.backend}})
		if err == nil || !strings.Contains(err.Error(), tc.why) || !strings.Contains(err.Error(), "vm") {
			t.Errorf("%v：要因为 %s 失败、原因里有后端名，得到 %v", tc.backend, tc.why, err)
		}
	}
}

func TestFirecrackerRegistersDisabledWithCapabilities(t *testing.T) {
	reg, err := parse(t, map[string]any{"default": "local", "backends": map[string]any{
		"local": map[string]any{"kind": "local", "isolation": "none"}, "firecracker": firecrackerOff}})
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(reg.Names(), ",") != "firecracker,local" || reg.Enabled("firecracker") || !reg.Enabled("local") {
		t.Fatalf("names %v，firecracker 停用、local 启用", reg.Names())
	}
	if !reg.HasCapability("firecracker", "snapshot") || reg.HasCapability("local", "snapshot") {
		t.Fatal("登记的能力查得到，没写的为空")
	}
}

func TestQuotaParsesNumbersFromThePolicy(t *testing.T) {
	p := FromDocument(map[string]any{"sandbox": map[string]any{"backend": "kata", "idle_timeout_s": 7200.0, "max_lifetime_s": 600.0,
		"quota": map[string]any{"max_concurrent": 3.0, "daily_cost_usd": 0.0, "monthly_cost_usd": 2.0}}}, "gvisor")
	if p.Backend != "kata" || p.Quota.MaxConcurrent != 3 || p.Quota.MonthlyCostUSD != 2 || p.Quota.DailyCostUSD != 0 {
		t.Fatalf("policy = %+v", p)
	}
	if p.IdleTimeout != 15*time.Minute {
		t.Fatalf("空闲时长只能调低部署的默认值：%v", p.IdleTimeout)
	}
	if p.MaxLifetime != 10*time.Minute {
		t.Fatalf("max_lifetime = %v", p.MaxLifetime)
	}
	if FromDocument(nil, "gvisor").Backend != "gvisor" {
		t.Fatal("没有策略时用部署的默认后端")
	}
}

func TestPolicyErrorsAreReported(t *testing.T) {
	reg, err := parse(t, map[string]any{"default": "gvisor", "backends": map[string]any{
		"gvisor": map[string]any{"kind": "local", "isolation": "container"}}})
	if err != nil {
		t.Fatal(err)
	}
	for doc, code := range map[string]string{
		`{"sandbox": {"backend": "gvisor", "fallback": ["firecracker-x"]}}`: "unknown_sandbox_backend",
		`{"sandbox": {"backend": "gvisor", "min_isolation": "vm"}}`:         "invalid_min_isolation",
	} {
		var parsed map[string]any
		_ = json.Unmarshal([]byte(doc), &parsed)
		ps := NewPolicies(reg, func(context.Context, string) (map[string]any, error) { return parsed, nil }, -1)
		if _, err := ps.For(context.Background(), "t1"); err == nil || !strings.Contains(err.Error(), code) {
			t.Errorf("%s：要 %s，得到 %v", doc, code, err)
		}
	}
}

func TestImagesMapRuntimesAndImageIsThePythonOne(t *testing.T) {
	// D-14：登记的 images 把运行环境映射到镜像；只给 image 时就是 python 的镜像（D-05 的写法）。
	reg, err := parse(t, map[string]any{"default": "k", "backends": map[string]any{
		"k":   map[string]any{"kind": "k8s", "isolation": "container", "image": "py:1", "images": map[string]any{"universal": "uni:1", "node": "node:1"}},
		"old": map[string]any{"kind": "k8s", "isolation": "container", "image": "py:0"}}})
	if err != nil {
		t.Fatal(err)
	}
	if reg.ImageFor("k", "universal") != "uni:1" || reg.ImageFor("k", "python") != "py:1" {
		t.Fatal("images 映射运行环境，image 是 python 的")
	}
	if reg.ImageFor("old", "python") != "py:0" || reg.ImageFor("old", "universal") != "" {
		t.Fatal("只给 image 的后端只有 python")
	}
}
