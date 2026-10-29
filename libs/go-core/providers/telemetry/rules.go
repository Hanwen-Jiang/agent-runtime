package telemetry

import (
	"slices"
	"strings"

	"github.com/agentruntime/agent-runtime/libs/go-core/secret"
)

// 各实现共用的属性规则（Python 侧见 agentruntime_core.telemetry_rules，两边一致）。
var (
	// SensitiveKeyParts：键名（不分大小写）含其中任何一个就脱敏。
	SensitiveKeyParts = []string{"password", "secret", "token", "api_key", "apikey", "authorization", "credential", "cookie"}
	// ReservedKeys：由绑定加上的租户与作用域属性；调用方传入的一律忽略。
	ReservedKeys = []string{"tenant_id", "user_id", "workspace_id", "session_id", "scope"}
	// GenAIUsageCountKeys：GenAI 语义约定的 token 计数，键名含 token，但值是整数时不是凭据，不脱敏（T-15）。
	GenAIUsageCountKeys = []string{"gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens"}
	// GenAITokenTypeKey：GenAI 指标 gen_ai.client.token.usage 的属性，键名含 token，值是 GenAITokenTypes 之一时不是凭据，不脱敏
	// （TMP 计划第 9.3 节）。
	GenAITokenTypeKey = "gen_ai.token.type"
	GenAITokenTypes   = []string{"input", "output"}
)

func isReserved(key string) bool {
	for _, k := range ReservedKeys {
		if k == key {
			return true
		}
	}
	return false
}

func isInteger(v any) bool {
	switch v.(type) {
	case int, int8, int16, int32, int64, uint, uint8, uint16, uint32, uint64:
		return true
	}
	return false
}

// IsSensitive 判断这个键（与值）是否要脱敏。
func IsSensitive(key string, value any) bool {
	for _, k := range GenAIUsageCountKeys {
		if k == key && isInteger(value) {
			return false
		}
	}
	if s, ok := value.(string); ok && key == GenAITokenTypeKey && slices.Contains(GenAITokenTypes, s) {
		return false
	}
	lowered := strings.ToLower(key)
	for _, s := range SensitiveKeyParts {
		if strings.Contains(lowered, s) {
			return true
		}
	}
	return false
}

func cleanValue(v any) any {
	switch x := v.(type) {
	case secret.Value:
		return Redacted
	case []secret.Value:
		return Redacted
	case []any:
		for _, e := range x {
			if _, ok := e.(secret.Value); ok {
				return Redacted
			}
		}
	}
	return v
}

// Sanitize 去掉保留属性，脱敏敏感键与 secret.Value。
func Sanitize(attrs []Attribute) map[string]any {
	out := map[string]any{}
	for _, a := range attrs {
		if isReserved(a.Key) {
			continue
		}
		if IsSensitive(a.Key, a.Value) {
			out[a.Key] = Redacted
			continue
		}
		out[a.Key] = cleanValue(a.Value)
	}
	return out
}
