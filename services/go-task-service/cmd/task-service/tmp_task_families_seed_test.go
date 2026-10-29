package main

// TMP 计划（docs/plan/eval-observability-and-runtime-fixes.md）第 6 节：开发种子里有平台默认的任务族词表
// （kind task_families，名为 default，不带租户），registry-seed 种下之后任何租户都读得到。

import (
	"bytes"
	"context"
	"path/filepath"
	"testing"

	"connectrpc.com/connect"

	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
	registryv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/registry/v1"
)

func TestTheDevSeedPlantsThePlatformTaskFamilies(t *testing.T) {
	dataDir := t.TempDir()
	devSeed := filepath.Join("..", "..", "..", "..", "deploy", "dev", "registry-seed.json")
	var out, errOut bytes.Buffer
	if code := run([]string{"registry-seed", devSeed}, &out, &errOut, seedEnv(dataDir)); code != 0 {
		t.Fatalf("开发种子应能种下，退出码 %d：%s", code, errOut.String())
	}
	reg := openSeeded(t, dataDir)
	resp, err := reg.GetTaskFamilies(context.Background(), connect.NewRequest(&registryv1.GetTaskFamiliesRequest{Tenant: &commonv1.TenantContext{TenantId: "gh-someone"}}))
	if err != nil {
		t.Fatalf("任何租户都读得到平台默认的词表：%v", err)
	}
	got := resp.Msg.GetTaskFamilies()
	if !got.GetPlatformDefault() || got.GetVersion() != "default/v1" {
		t.Errorf("是平台默认的第 1 版：%+v", got)
	}
	ids := map[string]bool{}
	for _, f := range got.GetDocument().GetValue().GetStructValue().GetFields()["families"].GetListValue().GetValues() {
		ids[f.GetStructValue().GetFields()["id"].GetStringValue()] = true
	}
	for _, want := range []string{"engineering.ci_fix", "engineering.code_explain", "conversation.chat"} {
		if !ids[want] {
			t.Errorf("词表里要有 %s：%v", want, ids)
		}
	}
}
