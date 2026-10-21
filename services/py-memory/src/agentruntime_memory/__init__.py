"""Memory Service（设计书 3.4、附录 I；计划“Memory Service 主体”、S-12b、S-13）。

- identity：认证身份。租户由服务端根据认证身份确定，请求里的租户上下文只用来校验一致。
- scopes：作用域 ACL（附录 I.1）：读写哪些作用域由身份决定，请求里不接受任何过滤条件。
- safety：写入的安全过滤（S-13）：PII 脱敏、密钥拒绝入库、检测指令式内容、外部来源降低信任。
- promotion：Promotion 规则（附录 I.4，S-13）。
- embedding：嵌入（经 Model Gateway；测试用确定的哈希嵌入）与稀疏向量。
- service：写入流水线、检索流水线、反馈、级联删除与墓碑、版本与回滚、人审队列。
- injection：检索结果以数据的形式注入上下文，标注来源与“可能过时”（S-13）。
- server、client：gRPC 服务（MemoryService）与客户端；python -m agentruntime_memory 独立运行。
"""
