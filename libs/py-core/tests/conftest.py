import pytest

# 一致性套件在包内，需要显式开启断言改写，失败时才有详细信息。
pytest.register_assert_rewrite("agentruntime_core.conformance")
