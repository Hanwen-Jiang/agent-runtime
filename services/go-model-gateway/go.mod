module github.com/agentruntime/agent-runtime/services/go-model-gateway

go 1.26.0

require (
	connectrpc.com/connect v1.21.0
	github.com/agentruntime/agent-runtime/libs/go-core v0.0.0-00010101000000-000000000000
	github.com/agentruntime/agent-runtime/proto/gen/go v0.0.0-00010101000000-000000000000
	google.golang.org/protobuf v1.36.12
)

// 同仓库的模块；go.work 用于构建，replace 让 go mod tidy 不去网络上找它们。
replace (
	github.com/agentruntime/agent-runtime/libs/go-core => ../../libs/go-core
	github.com/agentruntime/agent-runtime/proto/gen/go => ../../proto/gen/go
)
