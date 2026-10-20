"""S-03 验收：工作流代码必须确定（计划 S-03 难点“确定性（Python）”，附录 K.2）。

检查 agentruntime_worker/workflows 下的模块：不读系统时间、不用 random、不做 I/O、不调用模型，
不导入 activity 的实现（只能导入 activities.types）；不用 set 的遍历顺序做决定。
Temporal 的沙箱在运行时也会拦一部分，这里在代码层面提前拦。
"""

from __future__ import annotations

import ast
from pathlib import Path

import agentruntime_worker.workflows as workflows_pkg

WORKFLOW_DIR = Path(workflows_pkg.__file__).parent

FORBIDDEN_MODULES = {
    "time": "用 workflow.now()、workflow.sleep()",
    "random": "用 workflow.random()",
    "uuid": "用 workflow.uuid4()",
    "secrets": "用 workflow.random()",
    "os": "工作流不做 I/O",
    "pathlib": "工作流不做 I/O",
    "socket": "工作流不做 I/O",
    "subprocess": "工作流不做 I/O",
    "threading": "工作流不做 I/O",
    "urllib": "工作流不做 I/O",
    "http": "工作流不做 I/O",
    "requests": "工作流不做 I/O",
    "logging": "用 workflow.logger",
    "agentruntime_core": "Provider 调用放进 activity",
}
# 工作流代码里只允许导入 activity 的类型，不导入它们的实现（实现做 I/O、调用模型）。
FORBIDDEN_WORKER_MODULES = (
    "agentruntime_worker.activities.stubs",
    "agentruntime_worker.activities.calllog",
    "agentruntime_worker.config",
    # Node Executor 做 I/O；工作流只能用其中的纯函数模块 executor.keys（生成幂等键）。
    "agentruntime_worker.executor.node_executor",
    "agentruntime_worker.executor.dev",
    "agentruntime_worker.executor.model_gateway",
    "agentruntime_worker.executor.schemas",
)
FORBIDDEN_CALLS = {
    ("datetime", "now"): "改用 workflow.now()",
    ("datetime", "utcnow"): "改用 workflow.now()",
    ("datetime", "today"): "改用 workflow.now()",
    ("date", "today"): "改用 workflow.now()",
    # asyncio.wait 与 as_completed 用 set 返回结果，顺序不确定（计划 S-04：ready 集合要排序）。
    ("asyncio", "wait"): "改用 workflow.wait()",
    ("asyncio", "as_completed"): "改用 workflow.as_completed()",
}
FORBIDDEN_BUILTINS = {"open", "print", "input"}


def lint_source(source: str, filename: str) -> list[str]:
    tree = ast.parse(source, filename)
    problems: list[str] = []

    def report(node: ast.AST, message: str) -> None:
        problems.append(f"{filename}:{getattr(node, 'lineno', 0)}: {message}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            modules = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for module in modules:
                root = module.split(".")[0]
                if root in FORBIDDEN_MODULES:
                    report(node, f"工作流代码不能导入 {module}：{FORBIDDEN_MODULES[root]}")
                if module.startswith(FORBIDDEN_WORKER_MODULES):
                    report(node, f"工作流代码不能导入 {module}：只能导入 activities.types")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and (func.value.id, func.attr) in FORBIDDEN_CALLS:
                report(node, f"不用 {func.value.id}.{func.attr}()，{FORBIDDEN_CALLS[(func.value.id, func.attr)]}")
            if isinstance(func, ast.Name) and func.id in FORBIDDEN_BUILTINS:
                report(node, f"工作流代码不调用 {func.id}()")
        elif isinstance(node, ast.For | ast.AsyncFor | ast.comprehension):
            target = node.iter
            is_set = isinstance(target, ast.Set | ast.SetComp) or (
                isinstance(target, ast.Call) and isinstance(target.func, ast.Name) and target.func.id in {"set", "frozenset"}
            )
            if is_set:
                report(node, "不用 set 的遍历顺序做决定，先排序")
    return problems


def test_workflow_modules_are_deterministic() -> None:
    files = sorted(WORKFLOW_DIR.glob("*.py"))
    assert {"task.py", "stage_runner.py", "stage_plan.py"} <= {f.name for f in files}
    problems = [p for f in files for p in lint_source(f.read_text("utf-8"), f.name)]
    assert not problems, "\n".join(problems)


def test_lint_catches_violations() -> None:
    source = """
import asyncio, time, random
from datetime import datetime
from agentruntime_worker.activities.stubs import StubActivities
async def f(xs, tasks):
    datetime.now()
    open("x")
    await asyncio.wait(tasks)
    for x in set(xs):
        pass
    return [y for y in {1, 2}]
"""
    problems = "\n".join(lint_source(source, "bad.py"))
    for needle in ("time", "random", "datetime.now", "asyncio.wait", "activities.stubs", "open()", "set 的遍历顺序"):
        assert needle in problems, f"没有发现“{needle}”：\n{problems}"
