"""TMP 计划第 11.15 节，调优第一轮：环境构建失败之后。

SWE-bench 冒烟里查到的两个产品缺陷：
- 失败按定义的哈希永远记着：一次网络问题或一次放行配置错误，这个定义就再也建不成，只能改定义。过一段时间要允许重试。
- 失败原因只留前 500 个字符，而日志的结论在最后面：两次构建失败，都看不出为什么。要留尾部。
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from agentruntime_worker.sandbox.environments import Limits, workspace_reader

from .test_sandbox_environments import T1, TOOL_VERSIONS, FakeRegistry, registry, setup, write  # noqa: F401 — registry 是 fixture


async def failed_once(tmp_path: Path, registry: FakeRegistry, **limits: object):  # noqa: F811
    manager, builder, started, clock, _ = setup(tmp_path, registry, limits=Limits(**limits))
    root = write(tmp_path, {".tool-versions": TOOL_VERSIONS})
    building = await manager.resolve(T1, workspace_reader(root))
    builder.mode = "error"
    record = await manager.run_build("t1", building.key)
    assert record.state == "failed"
    return manager, builder, started, clock, root, building.key


async def test_a_failure_is_not_retried_right_away(tmp_path: Path, registry: FakeRegistry) -> None:  # noqa: F811
    """不能每次 shell_run 都起一次要跑很久的构建。"""
    manager, _, started, clock, root, _ = await failed_once(tmp_path, registry)
    clock.now += timedelta(minutes=10)
    again = await manager.resolve(T1, workspace_reader(root))
    assert again.state == "failed" and len(started) == 1


async def test_a_failure_can_be_retried_after_a_while(tmp_path: Path, registry: FakeRegistry) -> None:  # noqa: F811
    manager, builder, started, clock, root, key = await failed_once(tmp_path, registry)
    clock.now += Limits().failed_retry_after + timedelta(seconds=1)
    again = await manager.resolve(T1, workspace_reader(root))
    assert again.state == "building" and again.key == key and len(started) == 2
    builder.mode = "ok"
    record = await manager.run_build("t1", key)
    assert record.state == "ready", "这次成功了，就用得上"


async def test_the_retry_wait_is_configurable(tmp_path: Path, registry: FakeRegistry) -> None:  # noqa: F811
    manager, _, started, clock, root, _ = await failed_once(tmp_path, registry, failed_retry_after=timedelta(minutes=5))
    clock.now += timedelta(minutes=6)
    assert (await manager.resolve(T1, workspace_reader(root))).state == "building" and len(started) == 2


async def test_retrying_respects_the_image_quota(tmp_path: Path, registry: FakeRegistry) -> None:  # noqa: F811
    manager, _, started, clock, root, _ = await failed_once(tmp_path, registry, max_images=0)
    clock.now += Limits().failed_retry_after + timedelta(seconds=1)
    await manager.resolve(T1, workspace_reader(root))
    assert len(started) == 2, "max_images 为 0 表示不限，照样能重试"


async def test_the_end_of_a_long_failure_reason_is_kept(tmp_path: Path, registry: FakeRegistry) -> None:  # noqa: F811
    """真实的例子：reason 只留了开头的解压进度，错误本身被截掉了。"""
    manager, _, _, _, _, key = await failed_once(tmp_path, registry)
    await manager.store.mark_failed("t1", key, "构建失败：" + "#4 extracting sha256:abc\n" * 100 + "ERROR: no space left on device")
    record = await manager.store.get("t1", key)
    assert record is not None
    assert record.reason.endswith("ERROR: no space left on device")
    assert len(record.reason) <= 500
