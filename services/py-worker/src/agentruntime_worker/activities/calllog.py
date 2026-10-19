"""桩 activity 的调用记录（JSON Lines）。测试据此断言“已完成的 activity 不重跑”（计划 S-03 验收“杀 Worker”）。

每行一个对象：activity、task_id、event（start、heartbeat、cancelled、end、error）、attempt、task_queue、pid、ts。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from temporalio import activity

_lock = threading.Lock()


class CallLog:
    def __init__(self, path: Path | None) -> None:
        self._path = path

    def record(self, activity_name: str, task_id: str, event: str, **extra: Any) -> None:
        """追加一行；在 activity 里调用，attempt 与 task_queue 取自当前 activity 的信息。path 为 None 时不记。"""
        if self._path is None:
            return
        info = activity.info() if activity.in_activity() else None
        entry = {
            "activity": activity_name,
            "task_id": task_id,
            "event": event,
            "attempt": info.attempt if info else 0,
            "task_queue": info.task_queue if info else "",
            "pid": os.getpid(),
            "ts": time.time(),
            **extra,
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with _lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as f:
                f.write(line)
                f.flush()


def read_calls(path: Path) -> list[dict[str, Any]]:
    """读出全部记录；文件不存在时返回空列表。进程被强杀时可能留下半行，跳过它。"""
    if not path.exists():
        return []
    calls = []
    for line in path.read_text("utf-8").splitlines():
        if not line.strip():
            continue
        try:
            calls.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return calls
