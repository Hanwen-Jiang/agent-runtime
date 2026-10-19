// Package providererr 是统一的错误模型（计划 S-02 难点“错误语义统一”）。
//
// Python 与 Go 共用（Python 侧见 libs/py-core/src/agentruntime_core/errors.py），两边语义一致：
//   - Provider 实现只返回 *Error，不把底层库的错误直接交给调用方。
//   - 类别取自 proto 的 agentruntime.common.v1.ErrorCategory。
//   - 是否可重试只由类别决定：Transient 可重试，其余都不可重试。
//   - 错误文本里不得出现凭据、密钥或租户的原始数据。底层错误只经 Unwrap 取得，不进入 Error() 的文本。
package providererr

import (
	"errors"
	"time"

	commonv1 "github.com/agentruntime/agent-runtime/proto/gen/go/agentruntime/common/v1"
)

// Category 就是 proto 的 ErrorCategory。
type Category = commonv1.ErrorCategory

const (
	Transient = commonv1.ErrorCategory_ERROR_CATEGORY_TRANSIENT
	Invalid   = commonv1.ErrorCategory_ERROR_CATEGORY_INVALID
	Denied    = commonv1.ErrorCategory_ERROR_CATEGORY_DENIED
	NotFound  = commonv1.ErrorCategory_ERROR_CATEGORY_NOT_FOUND
	Conflict  = commonv1.ErrorCategory_ERROR_CATEGORY_CONFLICT
)

// Error 是所有 Provider 返回的错误。
type Error struct {
	Category Category
	// Code 是机器可读的细分原因，例如 missing_tenant、timeout、rate_limit。
	Code    string
	Message string
	// RetryAfter 只对 Transient 有意义：建议的最早重试间隔。
	RetryAfter time.Duration
	cause      error
}

// New 构造一个错误。
func New(category Category, code, message string) *Error {
	return &Error{Category: category, Code: code, Message: message}
}

// Wrap 构造一个错误并保留底层原因。原因的文本不会出现在 Error() 里。
func Wrap(category Category, code, message string, cause error) *Error {
	return &Error{Category: category, Code: code, Message: message, cause: cause}
}

func (e *Error) Error() string {
	if e.Message == "" {
		return e.Code
	}
	return e.Code + ": " + e.Message
}

func (e *Error) Unwrap() error { return e.cause }

// Retryable 只有 Transient 为 true。
func (e *Error) Retryable() bool { return e.Category == Transient }

// Proto 转为 proto 的 Error，供跨服务传递。
func (e *Error) Proto() *commonv1.Error {
	return &commonv1.Error{Category: e.Category, Code: e.Code, Message: e.Message}
}

// FromProto 从 proto 的 Error 还原。
func FromProto(p *commonv1.Error) *Error {
	return New(p.GetCategory(), p.GetCode(), p.GetMessage())
}

// CategoryOf 返回错误链上第一个 *Error 的类别；不是 Provider 错误时返回 UNSPECIFIED。
func CategoryOf(err error) Category {
	var e *Error
	if errors.As(err, &e) {
		return e.Category
	}
	return commonv1.ErrorCategory_ERROR_CATEGORY_UNSPECIFIED
}

// IsRetryable 判断错误是否可以重试。
func IsRetryable(err error) bool {
	return CategoryOf(err) == Transient
}
