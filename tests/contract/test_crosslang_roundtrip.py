"""S-01 验收：Go 编码的工作流载荷，Python 解码后字段完全一致；反向亦然。

Go 的 Session 工作流会启动 Python 的 Task 子工作流，两边的 Temporal payload converter 必须一致，
统一用 json/protobuf 编码（计划 S-01 难点“跨语言工作流载荷”）。

fixture 在 tests/contract/fixtures/crosslang/*.json，格式见同目录的 README.md。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from contract_tests import payloadconv
from contract_tests.paths import FIXTURES_DIR
from contract_tests.tools import run

CROSSLANG_DIR = FIXTURES_DIR / "crosslang"
TASK_WORKFLOW_INPUT = "agentruntime.workflow.v1.TaskWorkflowInput"

# 跨语言边界上的载荷：Go 的 Session 工作流发给 Python 的 Task 子工作流，或由 Go 的 Task Service 发出。
REQUIRED_TYPES = {
    TASK_WORKFLOW_INPUT,
    "agentruntime.workflow.v1.TaskWorkflowResult",
    "agentruntime.workflow.v1.SessionWorkflowInput",
    "agentruntime.workflow.v1.SubmitTaskSignal",
    "agentruntime.workflow.v1.HumanReplySignal",
}

# TaskWorkflowInput 的 fixture 必须覆盖这些容易在 JSON 编码中出错的值，否则“字段完全一致”说明不了问题。
REQUIRED_COVERAGE = {
    "map",  # 版本清单
    "repeated",
    "enum",  # 按名字编码
    "timestamp",
    "struct",  # JSON Schema 载荷
    "struct_list",
    "struct_null",
    "int64_beyond_double",  # int64 按字符串编码，超过 2^53 不丢精度
    "non_ascii_string",
}


def _fixtures() -> list[Path]:
    return sorted(CROSSLANG_DIR.glob("*.json"))


def test_fixtures_cover_boundary_types():
    types = {payloadconv.load_fixture(path).type for path in _fixtures()}
    missing = REQUIRED_TYPES - types
    assert not missing, f"缺少跨语言 fixture：{sorted(missing)}"


def test_task_workflow_input_fixture_exercises_tricky_encodings():
    inputs = [fx for fx in map(payloadconv.load_fixture, _fixtures()) if fx.type == TASK_WORKFLOW_INPUT]
    assert inputs, "缺少 TaskWorkflowInput 的 fixture"
    for fx in inputs:
        missing = REQUIRED_COVERAGE - payloadconv.coverage(fx.message)
        assert not missing, f"TaskWorkflowInput fixture 没有覆盖：{sorted(missing)}"


@pytest.mark.parametrize("fixture", _fixtures(), ids=lambda p: p.stem)
def test_python_roundtrip(fixture):
    fx = payloadconv.load_fixture(fixture)
    payload = payloadconv.encode(fx.message)
    payloadconv.assert_json_protobuf(payload, fx.type)
    decoded = payloadconv.decode(payload, fx.type)
    assert decoded == fx.message, payloadconv.diff(fx.message, decoded)


@pytest.mark.parametrize("fixture", _fixtures(), ids=lambda p: p.stem)
def test_go_encode_python_decode(fixture, payloadconv_go, tmp_path):
    out = tmp_path / "go.payload.json"
    run([payloadconv_go, "encode", "--fixture", fixture, "--out", out])
    fx = payloadconv.load_fixture(fixture)
    payload = payloadconv.read_payload(out)
    payloadconv.assert_json_protobuf(payload, fx.type)
    decoded = payloadconv.decode(payload, fx.type)
    assert decoded == fx.message, payloadconv.diff(fx.message, decoded)


@pytest.mark.parametrize("fixture", _fixtures(), ids=lambda p: p.stem)
def test_python_encode_go_decode(fixture, payloadconv_go, tmp_path):
    fx = payloadconv.load_fixture(fixture)
    payload = payloadconv.encode(fx.message)
    payloadconv.assert_json_protobuf(payload, fx.type)
    path = tmp_path / "python.payload.json"
    payloadconv.write_payload(payload, path)
    # verify 解码后与 fixture 用 proto.Equal 比较，不一致时以非零退出并打印两边内容。
    run([payloadconv_go, "verify", "--payload", path, "--fixture", fixture])
