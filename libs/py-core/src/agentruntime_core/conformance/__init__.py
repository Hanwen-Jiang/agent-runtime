"""Provider 一致性测试（计划 S-02）。每个接口一组，开发实现与生产实现都必须通过。

用法：在实现所在包的测试里继承对应的 XxxConformance，并提供 harness fixture：

    class TestInMemoryCache(CacheConformance):
        @pytest.fixture
        def harness(self):
            return MyCacheHarness(...)

harness 的协议见各模块的 XxxHarness。所有套件都覆盖：
- 租户绑定：缺少租户上下文 denied，格式不对 invalid，平台作用域必须写明用途，绑定时复制上下文。
- 租户隔离：A 写入，B 读不到；平台作用域与租户作用域互不可见。
- 错误分类：transient 可重试，其余不可重试；按接口逐项检查 not_found、conflict、invalid、denied 的触发条件。
- 幂等写，以及过期与删除语义（接口有这些语义时）。

套件在包内而不是测试目录里，使用前在 conftest.py 里调用
pytest.register_assert_rewrite("agentruntime_core.conformance")，以便断言失败时显示详细信息。
"""
