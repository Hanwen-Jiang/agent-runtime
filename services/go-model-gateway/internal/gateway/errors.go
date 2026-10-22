package gateway

import "github.com/agentruntime/agent-runtime/libs/go-core/connecterr"

// ErrorTrailer 是携带错误详情的 trailer（ar-error-bin），约定见 libs/go-core 的 connecterr 包（Registry 也用它）。
const ErrorTrailer = connecterr.Trailer

// RetryAfterDetail 是 common.v1.Error.details 里建议重试间隔的字段名（整数毫秒），见 connecterr。
const RetryAfterDetail = connecterr.RetryAfterDetail
