"""外部系统的替身（计划 S-07）：写操作落在 Worker 进程之外的文件里。

Worker 被杀、重启都不影响已经落地的记录；测试据此数“外部副作用真正生效了几次”。每次真正生效追加一行
（工具、幂等键、参数、结果、pid）。四个 ledger 工具代表外部 API 的三种能力（见 fault_worker.LEDGER_TOOLS）：

- 按 Idempotency-Key 去重：同一个键再次调用返回第一次的结果，不再生效。
- 支持按幂等键查询：可以问“这个键生效过没有”，但重复调用会重复生效。
- 两者都没有：重复调用就重复生效，也没法事后确认。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

_lock = threading.Lock()


class ExternalLedger:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "ledger.jsonl"

    def apply(self, tool: str, key: str | None, arguments: dict[str, Any], *, dedup: bool) -> dict[str, Any]:
        """执行一次写操作。dedup 为真时按幂等键去重（外部 API 支持 Idempotency-Key）。"""
        with _lock:
            if dedup and key:
                existing = self.find(tool, key)
                if existing is not None:
                    return existing["result"]
            seq = len(self.entries()) + 1
            result: dict[str, Any] = {"txn_id": f"txn-{seq}", "ref": arguments.get("ref", ""), "amount": arguments.get("amount", 0)}
            if arguments.get("echo_kb"):
                result["blob"] = "x" * (int(arguments["echo_kb"]) * 1024)
            entry = {"tool": tool, "key": key, "arguments": arguments, "result": result, "pid": os.getpid(), "ts": time.time()}
            self.root.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            return result

    def find(self, tool: str, key: str) -> dict[str, Any] | None:
        """按幂等键查第一次生效的记录。"""
        return next((e for e in self.entries() if e["tool"] == tool and e["key"] == key), None)

    def entries(self, ref: str | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text("utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:  # 进程被杀时可能留下半行；那一次没有落地
                continue
            if ref is None or entry["arguments"].get("ref") == ref:
                out.append(entry)
        return out

    def applied(self, ref: str) -> int:
        """ref 对应的写操作在外部真正生效的次数。"""
        return len(self.entries(ref))
