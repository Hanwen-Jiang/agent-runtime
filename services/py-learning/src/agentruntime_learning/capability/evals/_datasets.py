"""公开数据集 CellEval 的共用部分（docs/plan/capability-matrix-evals.md 第 3 节；BFCL v4、τ²-bench、RewardBench 2、JudgeBench、
LongBench v2、SimpleQA、FRAMES 都用它）。

- 数据集按钉死的版本下载到缓存目录 `AR_EVAL_DATA_DIR`（默认 `~/.cache/agentruntime-evals`），下载后核对 sha256；
  不随仓库分发数据集，测试用手写的小样本（同样的格式）。
- `select` 按 (seed, 题目 id) 的哈希排序选题：同一个 seed 选出同样的题，n 变大时只在后面追加（第 4 节“追加样本”）；
  给了分组时各组轮流取，各组题数尽量相等。
- 模型调用一律经传进来的 Model Gateway（`Meter.generate`）：租户 evals，`call.role` 是这道题的角色，花费取响应的 usage
  （失败时取错误里带的已计费花费）。
- 题目出错时 `Meter.error` 给 `ItemResult(score=0, error=...)`；被测角色没落在钉的模型上（PinningGateway.contaminated）时记 contaminated。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from google.protobuf import json_format, struct_pb2

from agentruntime.common.v1 import payload_pb2, tenant_pb2
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
from agentruntime_learning.capability.base import ItemResult, role_enum
from agentruntime_worker.executor.model_gateway import ModelGateway, billed_cost_usd

EVAL_TENANT = "evals"
DATA_ENV = "AR_EVAL_DATA_DIR"
USER_AGENT = "agentruntime-evals"
ERROR_LIMIT = 500


def data_dir() -> Path:
    """数据集缓存目录：`AR_EVAL_DATA_DIR`，没设时是 `~/.cache/agentruntime-evals`。"""
    return Path(os.environ.get(DATA_ENV) or Path.home() / ".cache" / "agentruntime-evals")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: Path, *, sha256: str | None = None, timeout: float = 300) -> Path:
    """下载到 dest（先写临时文件，核对 sha256 后再改名）；dest 已存在且哈希对得上时不再下载。"""
    dest = Path(dest)
    if dest.exists() and (sha256 is None or sha256_file(dest) == sha256):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=dest.name, suffix=".part")
    try:
        h = hashlib.sha256()
        with os.fdopen(fd, "wb") as out, urllib.request.urlopen(request, timeout=timeout) as resp:
            for chunk in iter(lambda: resp.read(1 << 20), b""):
                h.update(chunk)
                out.write(chunk)
        if sha256 is not None and h.hexdigest() != sha256:
            raise ValueError(f"{url} 的 sha256 是 {h.hexdigest()}，与钉死的 {sha256} 不同：数据集变了，先核对再改版本")
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return dest


def write_atomic(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in Path(path).read_text("utf-8").splitlines() if line.strip()]


def cached_jsonl(path: Path, build: Callable[[], Iterable[Mapping[str, Any]]]) -> list[dict[str, Any]]:
    """规整后的题目缓存（JSONL）：有就读，没有就 build 一次再写下。"""
    path = Path(path)
    if not path.exists():
        rows = [dict(r) for r in build()]
        write_atomic(path, "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows))
    return read_jsonl(path)


def _rank(seed: int, key: str) -> str:
    return hashlib.sha256(f"{seed}\x00{key}".encode()).hexdigest()


def select(ids: Sequence[str], n: int, seed: int, *, group: Callable[[str], str] | None = None) -> list[str]:
    """确定地选 n 道题（见模块说明）。n 大于题数时全给。"""
    if n < 0:
        raise ValueError("n 不能是负数")
    if len(set(ids)) != len(ids):
        raise ValueError("题目 id 有重复")
    if group is None:
        return sorted(ids, key=lambda i: _rank(seed, i))[:n]
    buckets: dict[str, list[str]] = {}
    for i in ids:
        buckets.setdefault(group(i), []).append(i)
    queues = [sorted(v, key=lambda i: _rank(seed, i)) for _, v in sorted(buckets.items())]
    out: list[str] = []
    while len(out) < n and any(queues):
        for q in queues:
            if q and len(out) < n:
                out.append(q.pop(0))
    return out


def value(data: Any) -> struct_pb2.Value:
    v = struct_pb2.Value()
    json_format.ParseDict(data, v)
    return v


def chat(role: str, text: str = "", *, tool_calls: Sequence[mg.ToolCall] = (), tool_call_id: str = "") -> mg.ChatMessage:
    roles = {"system": mg.CHAT_ROLE_SYSTEM, "user": mg.CHAT_ROLE_USER, "assistant": mg.CHAT_ROLE_ASSISTANT, "tool": mg.CHAT_ROLE_TOOL}
    return mg.ChatMessage(role=roles[role], text=text, tool_calls=list(tool_calls), tool_call_id=tool_call_id)


def tool_spec(name: str, description: str, parameters: Mapping[str, Any]) -> mg.ToolSpec:
    return mg.ToolSpec(name=name, description=description, params_schema=payload_pb2.JsonPayload(value=value(dict(parameters))))


def tool_call(call_id: str, name: str, arguments: Mapping[str, Any]) -> mg.ToolCall:
    out = mg.ToolCall(id=call_id, name=name)
    out.arguments.update(dict(arguments))
    return out


def arguments(call: mg.ToolCall) -> dict[str, Any]:
    """工具调用的参数（Struct 里的数都是浮点数，要整数的调用方自己按 schema 转）。"""
    return json_format.MessageToDict(call.arguments)


def messages_record(messages: Iterable[mg.ChatMessage]) -> list[dict[str, Any]]:
    """落盘用的消息（role、文字、工具调用）。"""
    out = []
    for m in messages:
        row: dict[str, Any] = {"role": mg.ChatRole.Name(m.role).removeprefix("CHAT_ROLE_").lower(), "text": m.text}
        if m.tool_calls:
            row["tool_calls"] = [{"id": c.id, "name": c.name, "arguments": arguments(c)} for c in m.tool_calls]
        if m.tool_call_id:
            row["tool_call_id"] = m.tool_call_id
        out.append(row)
    return out


def error_text(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= ERROR_LIMIT else text[: ERROR_LIMIT - 1] + "…"


class Meter:
    """一道题一次运行的模型调用：经 gateway 调，记下每次调用（角色、实际的模型、token、花费）。"""

    def __init__(self, gateway: ModelGateway, *, item_id: str, eval_name: str) -> None:
        self.gateway = gateway
        self.item_id = item_id
        self.eval_name = eval_name
        self.cost_usd = 0.0
        self.calls: list[dict[str, Any]] = []

    async def generate(
        self, *, role: str, node: str, messages: Sequence[mg.ChatMessage], tools: Sequence[mg.ToolSpec] = (),
        output_schema: Mapping[str, Any] | None = None, max_output_tokens: int = 0, tier: str = "", pinned: str = "",
        gateway: ModelGateway | None = None,
    ) -> mg.GenerateResponse:
        request = mg.GenerateRequest(
            tenant=tenant_pb2.TenantContext(tenant_id=EVAL_TENANT),
            call=mg.CallContext(task_id=f"{self.eval_name}:{self.item_id}", node_id=node, role=role_enum(role), tier=tier,
                                pinned_model_profile=pinned),
            generation=mg.GenerateParams(messages=list(messages), tools=list(tools), max_output_tokens=max_output_tokens),
        )
        if output_schema is not None:
            request.generation.output_schema.CopyFrom(payload_pb2.JsonPayload(value=value(dict(output_schema))))
        try:
            response = await (gateway or self.gateway).generate(request)
        except Exception as exc:
            cost = billed_cost_usd(exc)
            self.cost_usd += cost
            self.calls.append({"role": role, "node": node, "error": type(exc).__name__, "cost_usd": cost})
            raise
        usage = response.usage
        cost = usage.cost_usd_micros / 1_000_000
        self.cost_usd += cost
        self.calls.append({
            "role": role, "node": node, "model": response.model.model_profile_id if response.HasField("model") else "",
            "input_tokens": int(usage.input_tokens), "output_tokens": int(usage.output_tokens), "cost_usd": cost,
        })
        return response

    @property
    def contaminated(self) -> bool:
        return bool(getattr(self.gateway, "contaminated", False))

    def result(self, score: float, record: Mapping[str, Any]) -> ItemResult:
        return ItemResult(self.item_id, score=float(score), cost_usd=round(self.cost_usd, 6), contaminated=self.contaminated,
                          record={**record, "calls": self.calls})

    def error(self, exc: BaseException, record: Mapping[str, Any] | None = None) -> ItemResult:
        return ItemResult(self.item_id, score=0.0, cost_usd=round(self.cost_usd, 6), contaminated=self.contaminated,
                          error=error_text(exc), record={**(record or {}), "calls": self.calls})


def iter_json_array(path: Path, *, chunk_size: int = 1 << 22) -> Iterator[Any]:
    """逐个读出一个很大的 JSON 数组里的元素（LongBench v2 的 data.json 有 465 MB，不整个读进内存）。"""
    decoder = json.JSONDecoder()
    with Path(path).open("r", encoding="utf-8") as f:
        buf = f.read(chunk_size).lstrip()
        if not buf.startswith("["):
            raise ValueError(f"{path} 不是 JSON 数组")
        buf = buf[1:]
        eof = False
        while True:
            buf = buf.lstrip().removeprefix(",").lstrip()
            if buf.startswith("]"):
                return
            try:
                obj, end = decoder.raw_decode(buf)
            except json.JSONDecodeError:
                if eof:
                    raise
                more = f.read(chunk_size)
                eof = not more
                buf += more
                continue
            yield obj
            buf = buf[end:]
            if len(buf) < chunk_size and not eof:
                more = f.read(chunk_size)
                eof = not more
                buf += more


SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_name(text: str) -> str:
    return SAFE_ID.sub("_", text)[:120]


__all__ = [
    "DATA_ENV", "EVAL_TENANT", "Meter", "arguments", "cached_jsonl", "chat", "data_dir", "download", "error_text", "iter_json_array",
    "messages_record", "read_jsonl", "safe_name", "select", "sha256_file", "tool_call", "tool_spec", "value", "write_atomic",
]
