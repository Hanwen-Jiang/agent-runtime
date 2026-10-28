"""E4：在本机的 e4 整套上跑一批真 Task，让 Worker 收尾写回的 trace 经 Collector 进 Redpanda。

8 个 CI 修复（tests/e2e/live/workspaces/ci_rounding，同 T-13 的真模型端到端），3 个做不到的变体（测试里两条用例对同一输入要求不同结果，
又不许改测试）。同时最多跑 4 个。Task 提问（升级给人）时回复“做不到就结束”。结果写 <输出目录>/tasks.json。
用法：e4_tasks.py <整套目录> <输出目录>
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

REPO = Path(r"Z:\agentruntime\agent-runtime")
sys.path.insert(0, str(REPO / "tests" / "e2e"))
from e2e_support.api import ApiError, SseStream, TaskApi  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
ROOT, OUT = Path(sys.argv[1]), Path(sys.argv[2])
SAMPLE = REPO / "tests" / "e2e" / "live" / "workspaces" / "ci_rounding"
TENANT, USER = "t13", "u13"
MESSAGE = (
    "CI 挂了：在仓库根目录运行 `python -m unittest` 有用例失败。请找出原因并修好 pricing.py，让全部测试通过；"
    "不要改测试文件 test_pricing.py。改完再运行一遍 `python -m unittest` 确认。"
)
CONTRADICTION = '''

class WholesaleTest(unittest.TestCase):
    def test_half_a_cent_rounds_down_for_wholesale(self) -> None:
        self.assertEqual(line_total(5, 1, 50), 2)
'''
GIVE_UP = "测试文件不能改；如果用例之间互相矛盾、无法同时通过，就说明原因后直接结束这个任务，不要再尝试。"


def stack(*args: str) -> str:
    proc = subprocess.run([sys.executable, str(REPO / "scripts" / "dev_stack.py"), *args, "--root", str(ROOT)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr)
    return proc.stdout.strip().splitlines()[-1]


info = json.loads(stack("status", "--json"))
token = stack("token", "--tenant", TENANT, "--user", USER)
api = TaskApi(info["task_api"], token)
workspace_root = Path(info["workspace_root"])


def seed(name: str, impossible: bool) -> None:
    target = workspace_root / TENANT / name
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(SAMPLE, target, ignore=shutil.ignore_patterns("__pycache__"))
    if impossible:
        test = target / "test_pricing.py"
        text = test.read_text("utf-8").replace('\n\nif __name__ == "__main__":', CONTRADICTION + '\n\nif __name__ == "__main__":')
        test.write_text(text, "utf-8")


def run(name: str, impossible: bool) -> dict[str, Any]:
    seed(name, impossible)
    started = time.monotonic()
    sub = api.call("SubmitTask", {"client_request_id": uuid.uuid4().hex, "message": MESSAGE, "workspace_id": name, "session_access": "SESSION_ACCESS_READ_WRITE"})
    record: dict[str, Any] = {"workspace": name, "impossible": impossible, "task_id": sub["taskId"], "events": [], "replies": []}
    stream = SseStream(sub["eventsUrl"], token, timeout=2400)
    try:
        for ev in stream:
            record["events"].append(ev.event)
            text = json.dumps(ev.data, ensure_ascii=False)
            if "question_id" in text and ev.event != "task_finished":
                qid = _find(ev.data, "question_id")
                options = _find(ev.data, "options") or []
                option = next((o for o in (_name(o) for o in options) if any(k in o for k in ("abort", "reject", "stop", "cancel", "end"))), None) or (_name(options[0]) if options else "")
                try:
                    api.reply(sub["taskId"], qid, option, GIVE_UP)
                    record["replies"].append({"question_id": qid, "option": option, "options": [_name(o) for o in options]})
                except ApiError as err:
                    record["replies"].append({"question_id": qid, "error": f"{err.code} {err}"})
            if ev.event == "task_finished":
                payload = ev.data.get("payload", {}).get("value", {})
                record["status"] = payload.get("status") or payload.get("outcome")
                record["finished"] = payload
                break
    finally:
        stream.close()
    record["seconds"] = round(time.monotonic() - started, 1)
    q = api.query(sub["taskId"])
    record["query_status"] = q.get("status")
    print(f"{name}: {record.get('status')} / {record['query_status']} in {record['seconds']}s, replies={record['replies']}", flush=True)
    return record


def _find(data: Any, key: str) -> Any:
    if isinstance(data, dict):
        if key in data:
            return data[key]
        for v in data.values():
            found = _find(v, key)
            if found is not None:
                return found
    if isinstance(data, list):
        for v in data:
            found = _find(v, key)
            if found is not None:
                return found
    return None


def _name(option: Any) -> str:
    return str(option.get("id") or option.get("name") or option.get("value") or "") if isinstance(option, dict) else str(option)


jobs = [(f"e4-ok-{i}", False) for i in range(1, 9)] + [(f"e4-bad-{i}", True) for i in range(1, 4)]
# 交错着排：做不到的混在中间，不集中在最后。
jobs = [jobs[i] for i in (0, 8, 1, 2, 9, 3, 4, 10, 5, 6, 7)]
OUT.mkdir(parents=True, exist_ok=True)
with ThreadPoolExecutor(max_workers=4) as pool:
    results = list(pool.map(lambda j: run(*j), jobs))
(OUT / "tasks.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), "utf-8")
print(json.dumps({r["workspace"]: r.get("status") for r in results}, ensure_ascii=False))
