"""纠偏与立刻打断的记录（docs/plan/running-task-interaction.md 第 2 节）。Task 工作流的信号写它，Stage Runner 与 Round 循环在检查点读它。

- 纠偏写进 Task State 的 guidance（items 就是那个列表）：Planner 与 Reviewer 都看得到；撤回的直接拿掉，模型看不到。
- 工作流是单线程的，撤回与生效不会交错：谁的信号先到谁算。DurableEngine 只有信号没有 Update，结果靠事件 guidance_updated 告诉客户端；
  事件按发生的先后放进 outbox，由 Task 工作流的一个任务逐个推送（顺序不乱，也不让信号处理函数等 activity）。
- 同时跑着几个 tool_loop 时，纠偏对它们全都生效：每个循环各记看过哪些（LoopView），生效只算一次。
- 文字不进事件与日志。

工作流代码必须确定：纯数据结构，没有 I/O；set 只用来判断有没有，不遍历。
"""

from __future__ import annotations

from typing import Any

from agentruntime_worker.workflows.interrupts import NO_RESULT_NOTE, Scope

MAX_PENDING = 20
NEXT_STEP = "next_step"
INTERRUPT = "interrupt"
HELD_BACK_NOTE = "用户发来了纠偏，这个调用没有执行；看完纠偏再决定要不要做。"
# guidance_updated 的 reason_code。
TASK_FINISHED = "task_finished"
ALREADY_APPLIED = "already_applied"
NOT_FOUND = "not_found"
TOO_MANY_PENDING = "too_many_pending"
IRREVERSIBLE_IN_FLIGHT = "irreversible_in_flight"
EMPTY_TEXT = "empty_text"


class GuidanceBook:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        # 用过的 id（含撤回的）：客户端重发同一条不算新的一条。
        self._known: set[str] = set()
        self._retracted: set[str] = set()
        self.outbox: list[dict[str, Any]] = []
        # 正在跑的、能被打断的调用（interrupts.run）。
        self.scopes: list[Scope] = []
        self.closed = False

    def _find(self, guidance_id: str) -> dict[str, Any] | None:
        return next((g for g in self.items if g["guidance_id"] == guidance_id), None)

    def _say(self, guidance_id: str, state: str, *, mode: str = "", reason: str = "", round_seq: int = 0) -> None:
        event: dict[str, Any] = {"guidance_id": guidance_id, "state": state}
        if mode:
            event["mode"] = mode
        if reason:
            event["reason_code"] = reason
        if round_seq:
            event["applied_round"] = round_seq
        self.outbox.append(event)

    def pending(self) -> list[dict[str, Any]]:
        return [g for g in self.items if g["state"] == "pending"]

    def has_pending(self) -> bool:
        return bool(self.pending())

    def has_pending_interrupt(self) -> bool:
        return any(g["mode"] == INTERRUPT for g in self.pending())

    def submit(self, guidance_id: str, text: str, mode: str, *, in_flight: bool) -> bool:
        """收到一条。返回是不是新的一条立刻打断（调用方要去取消）。in_flight：有不可逆的调用正在跑，打断要等它结束。"""
        if guidance_id in self._known:
            return False
        if self.closed:
            self._say(guidance_id, "rejected", reason=TASK_FINISHED)
            return False
        if not text.strip():
            self._say(guidance_id, "rejected", reason=EMPTY_TEXT)
            return False
        if len(self.pending()) >= MAX_PENDING:
            self._say(guidance_id, "rejected", reason=TOO_MANY_PENDING)
            return False
        self._known.add(guidance_id)
        self.items.append({"guidance_id": guidance_id, "text": text, "mode": mode, "state": "pending"})
        self._say(guidance_id, "pending", mode=mode, reason=IRREVERSIBLE_IN_FLIGHT if mode == INTERRUPT and in_flight else "")
        return mode == INTERRUPT

    def edit(self, guidance_id: str, text: str) -> None:
        item = self._editable(guidance_id)
        if item is None:
            return
        if not text.strip():
            self._say(guidance_id, "rejected", mode=item["mode"], reason=EMPTY_TEXT)
            return
        item["text"] = text
        self._say(guidance_id, "pending", mode=item["mode"])

    def retract(self, guidance_id: str) -> None:
        if guidance_id in self._retracted:
            return
        item = self._editable(guidance_id)
        if item is None:
            return
        self.items.remove(item)
        self._retracted.add(guidance_id)
        self._say(guidance_id, "retracted", mode=item["mode"])

    def _editable(self, guidance_id: str) -> dict[str, Any] | None:
        """还没生效的纠偏可以撤回、修改；已经生效的不行；立刻打断发出去调用就取消了，也不行。"""
        item = self._find(guidance_id)
        if item is None:
            self._say(guidance_id, "rejected", reason=TASK_FINISHED if self.closed else NOT_FOUND)
            return None
        if self.closed:
            self._say(guidance_id, "rejected", mode=item["mode"], reason=TASK_FINISHED)
            return None
        if item["state"] != "pending" or item["mode"] == INTERRUPT:
            self._say(guidance_id, "rejected", mode=item["mode"], reason=ALREADY_APPLIED)
            return None
        return item

    def _apply(self, item: dict[str, Any], round_seq: int) -> None:
        if item["state"] == "pending":
            item["state"] = "applied"
            item["applied_round"] = round_seq
            self._say(item["guidance_id"], "applied", mode=item["mode"], round_seq=round_seq)

    def apply_pending(self, round_seq: int) -> None:
        """待生效的都生效（Planner 规划之前、回 Planner 的判定）。"""
        for item in self.pending():
            self._apply(item, round_seq)

    def close(self) -> None:
        """进入收尾：还没生效的拒绝（界面提示可以作为新任务发送），不留在 Task State 里；之后到的也拒绝。"""
        if self.closed:
            return
        self.closed = True
        for item in self.pending():
            self.items.remove(item)
            self._say(item["guidance_id"], "rejected", mode=item["mode"], reason=TASK_FINISHED)


class LoopView:
    """一个 tool_loop 看到的纠偏。开始之前已经生效的在 Task State 里，不用再告诉它。"""

    def __init__(self, book: GuidanceBook) -> None:
        self.book = book
        self._seen = {g["guidance_id"] for g in book.items if g["state"] == "applied"}

    def _unseen(self) -> list[dict[str, Any]]:
        return [g for g in self.book.items if g["guidance_id"] not in self._seen]

    def tell(self, round_seq: int) -> list[dict[str, Any]]:
        """下一次模型调用之前：这个循环还没看过的，按顺序写进 history，并且生效。"""
        entries = []
        for item in self._unseen():
            self._seen.add(item["guidance_id"])
            self.book._apply(item, round_seq)
            entries.append({"user_guidance": item["text"]})
        return entries

    def blocked(self, effect: str, *, writes: tuple[str, ...]) -> dict[str, Any] | None:
        """这个调用还能不能开始。还没看过立刻打断时都不开始；还没看过纠偏时写的不开始（用户可能正是要拦它），读的照跑。"""
        unseen = self._unseen()
        if any(g["mode"] == INTERRUPT for g in unseen):
            return {"error": NO_RESULT_NOTE, "interrupted": True}
        if unseen and effect in writes:
            return {"error": HELD_BACK_NOTE, "skipped": True}
        return None
