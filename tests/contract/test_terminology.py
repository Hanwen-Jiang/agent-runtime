"""术语检查：契约、代码、注释里不出现调研文档的旧叫法。

依据 CLAUDE.md“术语”与 docs/reference/runtime-quickref.md 第 7 节。附录 A 的枚举取值由
test_enum_parity.py 核对，这里只拦旧叫法。docs/ 不在检查范围内：设计书附录 A 的“原称”列本来就要列出旧叫法。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from pathlib import Path

from contract_tests.paths import EXAMPLES_DIR, REPO_ROOT

# 旧叫法 → 应使用的名称。
BANNED = {
    r"(?i)\borchestrator\b": "Planner",
    r"(?i)\bsynthesizer\b": "Reviewer",
    r"(?i)\bsuper[-_ ]?steps?\b": "Stage",
    r"\bread_external\b": "effect = read",
    r"\bps_plus\b": "reasoning = plan_first",
    r"\bself_discover\b": "reasoning = templated",
    r"\bsubflow\b": "mode = template",
    r"(?i)\btask[_ ]card\b": "Task Plan",
    # 作为取值出现的旧枚举名；散文里的 ReAct 等论文名不拦。
    r"[\"'](?:react|subagent)[\"']": "mode = tool_loop / sub_agent",
    r"[\"'](?:refine|cot)[\"']": "quality = revise / reasoning = stepwise",
    r"\bCacheQueue\b": "Cache",
    r"\bVectorIndex\b": "VectorStore",
    r"\bToolProvider\b": "ToolDriver",
    r"\bSecretProvider\b": "SecretStore",
    r"\bTaskJournal\b": "Temporal 工作流状态",
    r"\bSessionStore\b": "Temporal 工作流状态",
    r"\bMemoryStore\b": "Memory Service",
    "超步": "Stage",
    "精确节点": "Atomic Node",
    "抽象任务": "Goal Node",
    r"flow\s*配方": "Recipe",
    "账本": "Task State 或 Progress Check",
    "处置阶梯": "升级顺序（附录 B.8）",
    "节点模板库": "Node Catalog",
    "节点执行器": "Node Executor",
    "钩子": "Hook",
    "控制面": "Control Plane",
    "离线面": "Learning Pipeline",
    "注册中心": "Registry",
    "模型画像": "Model Profile",
    "上下文装配": "Context Builder",
    "压缩阶梯": "Compaction Ladder",
    "压缩质量探针": "Compaction Probe",
}
_PATTERNS = [(re.compile(pattern), replacement) for pattern, replacement in BANNED.items()]

SCANNED = ("proto", "schemas", "services", "libs", "deploy", "tests", "buf.yaml", "buf.gen.yaml", "go.work", "pyproject.toml")
TEXT_SUFFIXES = {".proto", ".json", ".py", ".pyi", ".go", ".yaml", ".yml", ".toml", ".md", ".mod", ".txt", ".sql"}
SKIP_DIRS = {".venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
SKIP_FILES = {"uv.lock", "go.sum"}
THIS_FILE = Path(__file__).resolve()


def _files() -> Iterator[Path]:
    for entry in SCANNED:
        root = REPO_ROOT / entry
        if root.is_file():
            yield root
        elif root.is_dir():
            # 跳过的目录不往下走（共享盘上别处留下的 .pytest_cache 可能读不了）。
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
                for name in sorted(filenames):
                    path = Path(dirpath) / name
                    if path.suffix in TEXT_SUFFIXES and name not in SKIP_FILES and path.resolve() != THIS_FILE:
                        yield path


def _text(path: Path) -> str:
    text = path.read_text("utf-8")
    if path.parent == EXAMPLES_DIR and path.name.endswith(".examples.json"):
        # 反例故意使用旧叫法以证明会被拒绝；只检查正例。
        return json.dumps(json.loads(text).get("valid", []), ensure_ascii=False)
    return text


def test_no_legacy_terms():
    hits = []
    for path in _files():
        text = _text(path)
        for pattern, replacement in _PATTERNS:
            for match in pattern.finditer(text):
                hits.append(f"{path.relative_to(REPO_ROOT).as_posix()}: {match.group(0)!r} → 用 {replacement}")
    assert not hits, "出现旧叫法：\n" + "\n".join(hits)
