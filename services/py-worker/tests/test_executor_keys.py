"""S-05 验收：幂等键与缓存键（计划 S-05 难点“幂等键”“缓存”；附录 D.1、D.4）。"""

from __future__ import annotations

import inspect
import re

from agentruntime_worker.executor import keys

IDK = re.compile(r"^idk_[0-9a-f]{64}$")


def test_idempotency_key_is_deterministic_and_well_formed() -> None:
    a = keys.idempotency_key("t1", "tsk_A", "pay_ticket", {"offer": "X1", "amount": 10})
    assert IDK.match(a)
    assert a == keys.idempotency_key("t1", "tsk_A", "pay_ticket", {"amount": 10, "offer": "X1"}), "键的顺序不影响"


def test_idempotency_key_depends_on_every_part() -> None:
    base = keys.idempotency_key("t1", "tsk_A", "pay", {"x": 1})
    assert len({
        base,
        keys.idempotency_key("t2", "tsk_A", "pay", {"x": 1}),
        keys.idempotency_key("t1", "tsk_B", "pay", {"x": 1}),
        keys.idempotency_key("t1", "tsk_A", "pay.1", {"x": 1}),
        keys.idempotency_key("t1", "tsk_A", "pay", {"x": 2}),
    }) == 5


def test_idempotency_key_has_no_attempt_number() -> None:
    """不含 attempt 编号，activity 重试与重放时不变（计划 S-05）。"""
    assert list(inspect.signature(keys.idempotency_key).parameters) == ["tenant_id", "task_id", "node_id", "inputs"]


def test_canonical_json_and_hash() -> None:
    assert keys.canonical_json({"b": 1, "a": ["华东", {"d": 2, "c": 3}]}) == '{"a":["华东",{"c":3,"d":2}],"b":1}'
    assert keys.input_hash({"b": 1, "a": 2}) == keys.input_hash({"a": 2, "b": 1})
    assert re.fullmatch(r"[0-9a-f]{64}", keys.input_hash({}))


def test_cache_key_includes_template_version_and_input_hash() -> None:
    k = keys.cache_key("sql_query@v3", {"quarter": "2026Q3"})
    assert k.startswith("node-cache:sql_query@v3:") and k.endswith(keys.input_hash({"quarter": "2026Q3"}))
    assert k != keys.cache_key("sql_query@v4", {"quarter": "2026Q3"}), "模板换了版本不能命中旧缓存"
    assert k != keys.cache_key("sql_query@v3", {"quarter": "2026Q2"})
