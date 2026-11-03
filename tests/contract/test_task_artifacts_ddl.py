"""F3（docs/plan/n18-file-delivery-f3.md）：task_artifacts 由 Worker 与 task-service 的迁移 207 建，谁先启动谁建，所以两边的建表语句必须一致。"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GO = REPO / "services" / "go-task-service" / "internal" / "taskapi" / "artifacts.go"
PY = REPO / "services" / "py-worker" / "src" / "agentruntime_worker" / "tools" / "deliver.py"


def _normal(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).replace("( ", "(").replace(" )", ")").strip()


def test_both_sides_create_task_artifacts_the_same_way() -> None:
    go = re.search(r"const ArtifactsDDL = `(.*?)`", GO.read_text("utf-8"), re.S)
    py = re.search(r'Migration\(207, "task_artifacts", \(\s*"""(.*?)"""', PY.read_text("utf-8"), re.S)
    assert go and py, "两边都要有迁移 207 的建表语句"
    assert _normal(go.group(1)) == _normal(py.group(1))
    assert 'Version: 207, Name: "task_artifacts"' in GO.read_text("utf-8"), "版本号与名字两边一致"
