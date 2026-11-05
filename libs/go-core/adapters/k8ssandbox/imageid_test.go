package k8ssandbox

import (
	"encoding/json"
	"strings"
	"testing"
)

// D-14：环境表记后端报的实际镜像（按 digest）：K8s 取沙箱容器的 imageID，去掉运行时加的前缀。
func TestK8sReportsTheImageIDOfTheSandboxContainer(t *testing.T) {
	digest := "ghcr.io/agentruntime/sandbox-python@sha256:" + strings.Repeat("1", 64)
	var pd pod
	raw := `{"status": {"containerStatuses": [{"name": "other", "imageID": "x@sha256:` + strings.Repeat("0", 64) + `"},
	  {"name": "sandbox", "imageID": "docker-pullable://` + digest + `"}]}}`
	if err := json.Unmarshal([]byte(raw), &pd); err != nil {
		t.Fatal(err)
	}
	if got := imageID(&pd); got != digest {
		t.Fatalf("imageID = %q", got)
	}
	if got := imageID(&pod{}); got != "" {
		t.Fatalf("没有容器状态时为空：%q", got)
	}
}
