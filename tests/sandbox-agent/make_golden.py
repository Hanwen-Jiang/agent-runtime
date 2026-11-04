"""用 Go 版 agent 导出 fixture_tree 的存档与清单，写进 golden/（D-28 验收 2）。只在有意更新 golden 时跑，结果人审后提交：

    uv run --package agentruntime-sandbox-agent-tests python tests/sandbox-agent/make_golden.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixture_tree  # noqa: E402
from conftest import _build, start_agent, stop_agent  # noqa: E402

GOLDEN = Path(__file__).resolve().parent / "golden"


def main() -> None:
    if sys.platform != "linux":
        sys.exit("golden 要在 Linux 上生成（符号链接与权限位）")
    os.umask(0o022)
    with tempfile.TemporaryDirectory() as d:
        exe = _build("go", Path(d))
        work = Path(d) / "work"
        work.mkdir()
        fixture_tree.make(work)
        agent = start_agent(exe, work, impl="go")
        try:
            res = agent.request("GET", "/v1/archive?max_bytes=1048576")
            assert res.status == 200, res.body
            man = agent.request("GET", "/v1/manifest")
            assert man.status == 200
        finally:
            stop_agent(agent)
    GOLDEN.mkdir(exist_ok=True)
    (GOLDEN / "go-export.tar").write_bytes(res.body)
    (GOLDEN / "go-export.manifest.json").write_bytes(man.body)
    print(f"写了 {GOLDEN}：{len(res.body)} 字节，{len(json.loads(man.body)['entries'])} 项")


if __name__ == "__main__":
    main()
