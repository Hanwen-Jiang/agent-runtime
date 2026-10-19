// Package devutil 是开发实现共用的小工具。
package devutil

import (
	"regexp"
	"time"

	"github.com/agentruntime/agent-runtime/libs/go-core/providererr"
	"github.com/agentruntime/agent-runtime/libs/go-core/tenancy"
)

// PlatformID 是平台作用域的标识。租户 ID 不能以下划线开头，不会与它冲突。
const PlatformID = "_platform"

var name = regexp.MustCompile(`^[^\s\x00-\x1f\x7f]{1,512}$`)

// ScopeID 返回作用域的稳定标识：租户 ID，平台作用域为 _platform。
func ScopeID(s tenancy.Scope) string {
	if s.IsPlatform() {
		return PlatformID
	}
	return s.TenantID
}

// CheckName 校验键、锁名、流名：1 到 512 个字符，不含空白与控制字符。
func CheckName(value, what string) error {
	if !name.MatchString(value) {
		return providererr.New(providererr.Invalid, "invalid_argument", what+" 必须是 1 到 512 个不含空白与控制字符的字符")
	}
	return nil
}

// CheckPositive 校验时长大于 0。
func CheckPositive(d time.Duration, what string) error {
	if d <= 0 {
		return providererr.New(providererr.Invalid, "invalid_argument", what+" 必须大于 0")
	}
	return nil
}
