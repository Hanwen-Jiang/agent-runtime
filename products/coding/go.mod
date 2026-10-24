module github.com/agentruntime/agent-runtime/products/coding

go 1.26.0

require github.com/agentruntime/agent-runtime/libs/go-core v0.0.0-00010101000000-000000000000

require (
	github.com/agentruntime/agent-runtime/proto/gen/go v0.0.0-00010101000000-000000000000 // indirect
	google.golang.org/protobuf v1.36.12 // indirect
)

replace (
	github.com/agentruntime/agent-runtime/libs/go-core => ../../libs/go-core
	github.com/agentruntime/agent-runtime/proto/gen/go => ../../proto/gen/go
)
