"""Durable, single-owner spending guard for Model Gateway calls (standard library only).

Wrap inside retrying clients so every provider attempt consumes the same cap.
Call close() after use. A per-path OS lock rejects another runner; crash releases
the lock but leaves pending billing unknown until reconciled against the ledger.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any


class BudgetError(RuntimeError):
    def __init__(self, message: str, *, billed_cost_usd_micros: int | None = None) -> None:
        super().__init__(message)
        # None: a provider call may have incurred unknown cost. Zero: no call made.
        self.billed_cost_usd_micros = billed_cost_usd_micros


class BudgetGateway:
    def __init__(self, inner: Any, path: Path, budget_usd: float, *, prior_cost_usd: float = 0) -> None:
        if not math.isfinite(budget_usd) or budget_usd <= 0:
            raise ValueError("budget-usd must be finite and positive")
        if not math.isfinite(prior_cost_usd) or prior_cost_usd < 0:
            raise ValueError("prior cost must be finite and nonnegative")
        self.inner, self.path = inner, Path(path)
        self.limit = int(round(budget_usd * 1e6))
        if self.limit <= 0:
            raise ValueError("budget must be at least one microdollar")
        self.lock = asyncio.Lock()
        self._owner = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._claim()
        try:
            self.state = {"limit_usd_micros": self.limit, "spent_usd_micros": int(round(prior_cost_usd * 1e6)), "pending": False}
            if self.path.exists():
                self.state = json.loads(self.path.read_text("utf-8"))
                if self.state.get("limit_usd_micros") != self.limit:
                    raise ValueError("budget changed; retain the approved original cap")
                if (not isinstance(self.state.get("spent_usd_micros"), int) or self.state["spent_usd_micros"] < 0
                        or not isinstance(self.state.get("pending"), bool)):
                    raise ValueError("invalid persisted budget state")
                if self.state["spent_usd_micros"] < int(round(prior_cost_usd * 1e6)):
                    raise ValueError("budget state is behind recorded costs; reconcile before resuming")
            self._save()
        except BaseException:
            self.close()
            raise

    def _claim(self) -> None:
        # Never unlink this file: replacing the inode would bypass another owner.
        path = self.path.with_name(self.path.name + ".lock")
        f = open(path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                if f.tell() == 0:
                    f.write(b"0"); f.flush()
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            raise BudgetError("budget already has an active owner", billed_cost_usd_micros=0) from None
        self._owner = f

    def close(self) -> None:
        if self.lock.locked():
            raise RuntimeError("cannot close budget during an active call")
        if self._owner is not None:
            self._owner.close()
            self._owner = None

    def __del__(self) -> None:
        owner = getattr(self, "_owner", None)
        if owner is not None:
            owner.close()

    def _save(self) -> None:
        fd, tmp = tempfile.mkstemp(prefix=self.path.name + ".", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2)
                f.write("\n"); f.flush(); os.fsync(f.fileno())
            os.replace(tmp, self.path)
            if os.name != "nt":
                directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    async def generate(self, request: Any) -> Any:
        return await self._call("generate", request)

    async def embed(self, request: Any) -> Any:
        return await self._call("embed", request)

    async def _call(self, method: str, request: Any) -> Any:
        async with self.lock:
            if self._owner is None:
                raise BudgetError("budget owner is closed", billed_cost_usd_micros=0)
            if self.state["pending"]:
                raise BudgetError("cost unknown; reconcile pending call before retry", billed_cost_usd_micros=0)
            remaining = self.limit - self.state["spent_usd_micros"]
            if remaining <= 0:
                raise BudgetError("budget exhausted", billed_cost_usd_micros=0)
            call = type(request).FromString(request.SerializeToString())
            call.call.max_cost_usd_micros = min(call.call.max_cost_usd_micros or remaining, remaining)
            self.state["pending"] = True
            self._save()
            try:
                result = await getattr(self.inner, method)(call)
            except Exception as exc:
                billed = getattr(exc, "billed_cost_usd_micros", None)
                if isinstance(billed, int) and billed >= 0:
                    self.state["spent_usd_micros"] += billed
                    self.state["pending"] = False
                self._save()
                raise
            if not result.HasField("usage") or result.usage.cost_usd_micros < 0:
                raise BudgetError("response has no valid billing evidence")
            self.state["spent_usd_micros"] += int(result.usage.cost_usd_micros)
            self.state["pending"] = False
            self._save()
            return result
