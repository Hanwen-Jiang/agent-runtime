"""Runtime 的 Python 共享库（强档实施计划 S-02）。

- errors：统一的错误模型，映射到 proto 的 ErrorCategory。
- tenancy：租户绑定。Provider 客户端按请求绑定租户上下文，之后的调用不再传租户。
- secrets：不会出现在日志、trace 与错误信息里的 Secret 值。
- providers：Python 侧需要的 Provider 接口（设计书第 4 章）。
- conformance：所有实现都必须通过的一致性测试。
"""
