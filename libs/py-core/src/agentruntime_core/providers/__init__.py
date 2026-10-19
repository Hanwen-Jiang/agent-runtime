"""Python 侧的 Provider 接口（设计书第 4 章；计划 S-02）。

按设计书 2.3 的语言划分：

| 接口 | Python | Go | 说明 |
| --- | --- | --- | --- |
| DurableEngine | ✓ | ✓ | 客户端操作：启动、信号、查询、取消。工作流代码本身直接用 Temporal SDK |
| RelationalStore | ✓ | ✓ | |
| VectorStore | ✓ | | Memory Service |
| Cache | ✓ | ✓ | |
| EventStream | ✓ | | Learning Pipeline |
| ObjectStore | ✓ | ✓ | |
| ModelProvider | | ✓ | 只在 Model Gateway 里用；Python 经 gRPC 调 Model Gateway |
| ToolDriver | ✓ | | program 节点 |
| Sandbox | ✓ | | program 节点 |
| HumanChannel | ✓ | | human 节点与 Reviewer 问人 |
| SecretStore | ✓ | ✓ | |
| Telemetry | ✓ | ✓ | |
| Ingress | | ✓ | Task Service 的入口 |

两种语言都有的接口，文件头注明，方法一一对应（snake_case ↔ CamelCase），语义一致，由
tests/contract/test_provider_parity.py 比对方法清单。

约定（每个接口都遵守）：

- 接口只表达能力，不出现具体产品的概念。
- 工厂 XxxProvider 长期存在；按请求 bind(tenant) 得到绑定租户的客户端 Xxx，见 agentruntime_core.tenancy。
- 方法一律 async；只抛 agentruntime_core.errors.ProviderError。
- 跨租户访问一律当作不存在（not_found），或在引用里带了别的租户时报 denied，不泄露别的租户的数据。
"""
