// Package gocore 是 Runtime 的 Go 共享库（强档实施计划 S-02）。
//
//   - providererr：统一的错误模型，映射到 proto 的 ErrorCategory。
//   - tenancy：租户绑定。Provider 客户端按请求绑定租户上下文，之后的调用不再传租户。
//   - secret：不会出现在日志、trace 与错误信息里的密钥值。
//   - providers/...：Go 侧需要的 Provider 接口（设计书第 4 章）。
//   - conformance/...：所有实现都必须通过的一致性测试。
//
// 按设计书 2.3 的语言划分，Go 侧有 DurableEngine、RelationalStore、Cache、ObjectStore、SecretStore、
// Telemetry、ModelProvider、Ingress 八个接口；其中前六个两种语言都有，Python 侧见 libs/py-core，
// 方法一一对应（CamelCase ↔ snake_case），语义一致，由 tests/contract/test_provider_parity.py 比对。
package gocore
