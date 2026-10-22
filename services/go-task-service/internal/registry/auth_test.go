package registry_test

// T-08 验收：写权限（docs/plan/registry.md 第 2 节第 7 条、第 4 节“令牌不外泄”）。Registry 在内部端口上，读方法不认证
// （服务间认证留给部署层的 mTLS）；Publish、Promote 另要发布令牌（gRPC 元数据 authorization: Bearer …），令牌存在
// SecretStore 的平台作用域。令牌缺了或不对一律 denied，不说明是哪一种；SecretStore 里没有令牌时一律拒绝（fail-closed）。
// 令牌不出现在错误与日志里。

import (
	"path/filepath"
	"strings"
	"testing"

	"connectrpc.com/connect"

	"github.com/agentruntime/agent-runtime/libs/go-core/dev/filesecrets"

	"github.com/agentruntime/agent-runtime/services/go-task-service/internal/registry"
)

func TestPublishAndPromoteRequireThePublishToken(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		_, missingErr := h.publishAs("", nil, kindTemplate, "answer", answerDoc)
		wantCode(t, "没有令牌时发布", missingErr, connect.CodePermissionDenied)
		_, wrongErr := h.publishAs("wrong-token", nil, kindTemplate, "answer", answerDoc)
		wantCode(t, "令牌不对时发布", wrongErr, connect.CodePermissionDenied)
		if errorMessage(missingErr) != errorMessage(wrongErr) {
			t.Errorf("缺令牌与令牌不对的说明一样，不透露是哪一种：%q / %q", missingErr, wrongErr)
		}
		if _, err := h.getTemplate(t1, "answer", "v1"); connect.CodeOf(err) != connect.CodeNotFound {
			t.Errorf("被拒的发布没有留下版本：%v", err)
		}

		v := h.mustPublish(nil, kindTemplate, "answer", answerDoc)
		_, err := h.promoteAs("", nil, kindTemplate, "answer", v, passingEvidence)
		wantCode(t, "没有令牌时晋升", err, connect.CodePermissionDenied)
		_, err = h.promoteAs("wrong-token", nil, kindTemplate, "answer", v, passingEvidence)
		wantCode(t, "令牌不对时晋升", err, connect.CodePermissionDenied)
		if got := h.mustGetTemplate(t1, "answer", v); got.GetState() != stateShadow {
			t.Errorf("被拒的晋升没有改状态：%s", got.GetState())
		}

		// 读方法不要令牌。
		h.mustPromote(nil, kindTemplate, "answer", v)
		if _, err := h.getTemplate(t1, "answer", ""); err != nil {
			t.Errorf("读方法不要令牌：%v", err)
		}
	})
}

func TestPublishTokenNeverAppearsInErrorsOrLogs(t *testing.T) {
	eachBackend(t, func(t *testing.T, h *harness) {
		// 用对的令牌发一个不合格的文档，用一个“差一点对”的令牌发一个合格的文档：两条错误与日志里都不能有令牌。
		_, invalidErr := h.publish(nil, kindTemplate, "answer", `{"executor":"model"}`)
		wantCode(t, "不合格的文档", invalidErr, connect.CodeInvalidArgument)
		nearMiss := publishToken + "-x"
		_, deniedErr := h.publishAs(nearMiss, nil, kindTemplate, "answer", answerDoc)
		wantCode(t, "令牌不对", deniedErr, connect.CodePermissionDenied)
		_, promoteErr := h.promoteAs(nearMiss, nil, kindTemplate, "answer", "v1", passingEvidence)
		wantCode(t, "令牌不对时晋升", promoteErr, connect.CodePermissionDenied)

		for _, err := range []error{invalidErr, deniedErr, promoteErr} {
			if strings.Contains(err.Error(), publishToken) {
				t.Errorf("错误里出现了发布令牌：%v", err)
			}
		}
		if logs := h.logs.String(); strings.Contains(logs, publishToken) {
			t.Errorf("日志里出现了发布令牌：\n%s", logs)
		}
	})
}

func TestPublishIsDeniedWhenNoTokenIsConfigured(t *testing.T) {
	for _, b := range backends {
		t.Run(b.name, func(t *testing.T) {
			empty, err := filesecrets.New(filesecrets.Options{Path: filepath.Join(t.TempDir(), "secrets.json")})
			if err != nil {
				t.Fatal(err)
			}
			h := newHarness(t, b.open(t), func(o *registry.Options) { o.Secrets = empty })
			for _, token := range []string{"", publishToken, "anything"} {
				_, err := h.publishAs(token, nil, kindTemplate, "answer", answerDoc)
				wantCode(t, "SecretStore 里没有发布令牌时发布（令牌 "+map[bool]string{true: "空", false: "非空"}[token == ""]+"）", err, connect.CodePermissionDenied)
			}
			if _, err := h.getTemplate(t1, "answer", ""); connect.CodeOf(err) != connect.CodeNotFound {
				t.Errorf("读方法照常可用（没有发布过，所以是 NotFound）：%v", err)
			}
		})
	}
}
