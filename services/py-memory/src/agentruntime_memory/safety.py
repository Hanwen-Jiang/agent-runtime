"""写入的安全过滤（附录 I.4 第 4 步、I.7“防投毒”；计划 S-13）。

规则由代码判定，不调用模型：
- PII 脱敏：邮箱、手机号、身份证号（校验位）、银行卡号（Luhn）、IPv4，换成占位符；条目的 sensitivity 记为 pii。
- 密钥拒绝入库：常见的密钥前缀（sk-、ghp_、AKIA、xox…）、PEM 私钥、JWT、password=/token= 赋值、长的高熵串；
  sensitivity 声明为 secret 的也拒绝。
- 指令式内容：要求 agent 执行、改变行为或忽略约束的写法（中英文）、管道执行的 shell 命令等；非外部来源的进人审，
  外部来源的直接拒绝（外部内容不得含指令）。
- 外部来源（provenance.source = external_content）：信任压到 EXTERNAL_TRUST_CAP 以下，且不可晋升。

只检查正文（标题、指导、理由）；证据里的 trace 等标识不算。
"""

from __future__ import annotations

import copy
import math
import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

ACTIONS = ("accept", "review", "reject")
EXTERNAL_TRUST_CAP = 0.2
TEXT_FIELDS = ("title", "guidance", "rationale")


@dataclass(frozen=True)
class Screening:
    action: str
    """accept、review（进人审，人审通过前检索不到）或 reject（不入库）。"""
    item: dict[str, Any]
    """处理后的条目（已脱敏、已调整信任）；reject 时是原条目。"""
    reasons: tuple[str, ...] = field(default_factory=tuple)
    """判定依据（写入审计日志）；不含命中的原文。"""


# --- PII ---

_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)


def _id_number_ok(text: str) -> bool:
    total = sum(int(c) * w for c, w in zip(text[:17], _ID_WEIGHTS, strict=True))
    return "10X98765432"[total % 11] == text[17].upper()


def _luhn_ok(text: str) -> bool:
    digits = [int(c) for c in re.sub(r"\D", "", text)]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d *= 2
            d -= 9 if d > 9 else 0
        total += d
    return total % 10 == 0


_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
# （类别，模式，额外校验）；按顺序替换，身份证在银行卡之前。
_PII: list[tuple[str, re.Pattern[str], Callable[[str], bool] | None]] = [
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"), None),
    ("id_number", re.compile(r"(?<![0-9A-Za-z])\d{17}[\dXx](?![0-9A-Za-z])"), _id_number_ok),
    ("bank_card", re.compile(r"(?<![\d-])\d(?:[ -]?\d){12,18}(?![\d-])"), _luhn_ok),
    ("phone", re.compile(r"(?<!\d)(?:\+?86[ -]?)?1[3-9]\d{9}(?!\d)"), None),
    ("ipv4", re.compile(rf"(?<![\d.]){_OCTET}(?:\.{_OCTET}){{3}}(?![\d.])"), None),
]


def redact_pii(text: str) -> tuple[str, list[str]]:
    """返回（脱敏后的文本，命中的类别）。"""
    found: list[str] = []
    for category, pattern, check in _PII:

        def replace(m: re.Match[str], category: str = category, check: Callable[[str], bool] | None = check) -> str:
            if check is not None and not check(m.group(0)):
                return m.group(0)
            if category not in found:
                found.append(category)
            return f"<{category}>"

        text = pattern.sub(replace, text)
    return text, found


# --- 密钥 ---

_SECRETS: list[tuple[str, re.Pattern[str]]] = [
    ("api_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{10,}")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}")),
    ("credential_assignment", re.compile(r"(?i)\b(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret)\s*[:=]\s*\S{4,}")),
]
_LONG_TOKEN = re.compile(r"[A-Za-z0-9+/_=-]{32,}")
_ENTROPY_BITS = 4.2  # 32 位以上、每字符超过 4.2 比特：十六进制串（最多 4 比特）不算


def _entropy(text: str) -> float:
    counts = Counter(text)
    return -sum(c / len(text) * math.log2(c / len(text)) for c in counts.values())


def find_secrets(text: str) -> list[str]:
    """命中的密钥类别；不返回密钥本身。"""
    found = [category for category, pattern in _SECRETS if pattern.search(text)]
    for token in _LONG_TOKEN.findall(text):
        if re.search(r"[A-Za-z]", token) and re.search(r"\d", token) and _entropy(token) > _ENTROPY_BITS:
            found.append("high_entropy")
            break
    return found


# --- 指令式内容 ---

_INSTRUCTIONS: list[tuple[str, re.Pattern[str]]] = [
    ("override_instructions", re.compile(r"(?i)\b(?:ignore|forget|override)\s+(?:all\s+|any\s+)?(?:the\s+)?(?:previous|prior|above|earlier|existing)\s+(?:instructions|rules|prompts?|guidelines)")),
    ("override_instructions", re.compile(r"(?i)\bdisregard\s+(?:all\s+|any\s+)?(?:the\s+)?(?:previous\s+|prior\s+)?(?:instructions|rules|safety|polic(?:y|ies)|guidelines|constraints)")),
    ("override_instructions", re.compile(r"(?:忽略|无视|忘记|忘掉)(?:掉)?(?:之前|以前|上面|先前|前面|所有|全部)?的?(?:规则|指令|指示|要求|约束|设定|限制)")),
    ("role_manipulation", re.compile(r"(?i)\b(?:system\s+prompt|developer\s+mode|jailbreak|you\s+are\s+now)\b")),
    ("role_manipulation", re.compile(r"你(?:必须|现在是|要扮演|应当无条件)")),
    ("persistent_rule", re.compile(r"(?i)\bfrom\s+now\s+on\b")),
    ("persistent_rule", re.compile(r"从(?:现在|今天|此)(?:起|开始)")),
    ("persistent_rule", re.compile(r"(?:以后|今后|往后)[^。\n]{0,20}(?:一律|都要|必须|总是|一定要)")),
    ("bypass_approval", re.compile(r"(?i)\bwithout\s+(?:asking|confirmation|approval|telling)\b|\bapprove\s+(?:every|all|any)\b")),
    ("bypass_approval", re.compile(r"(?:不要|无需|不用|不必)(?:再)?(?:问|询问|告诉|通知|确认|请示)")),
    ("bypass_approval", re.compile(r"直接(?:执行|付款|转账|删除|批准|审批通过)")),
    ("remote_execution", re.compile(r"(?i)\b(?:curl|wget)\b[^|\n]*\|\s*(?:sudo\s+)?(?:ba|z)?sh\b")),
    ("exfiltration", re.compile(r"(?i)\b(?:send|upload|post|exfiltrate|forward)\s+(?:all\s+|every\s+)?(?:the\s+)?(?:files|data|secrets|credentials|tokens)\b")),
    ("exfiltration", re.compile(r"(?:所有|全部)(?:的)?(?:文件|数据|密钥|凭据)(?:都)?(?:发|传|上传|转发)到")),
]


def instruction_signals(text: str) -> list[str]:
    """命中的指令式写法类别。"""
    found: list[str] = []
    for category, pattern in _INSTRUCTIONS:
        if category not in found and pattern.search(text):
            found.append(category)
    return found


# --- 综合判定 ---


def _texts(item: Mapping[str, Any]) -> list[str]:
    content = item.get("content") if isinstance(item.get("content"), Mapping) else {}
    return [content[k] for k in TEXT_FIELDS if isinstance(content.get(k), str)]


def screen(item: Mapping[str, Any]) -> Screening:
    original = copy.deepcopy(dict(item))
    if item.get("sensitivity") == "secret":
        return Screening("reject", original, ("declared_secret",))
    texts = _texts(item)
    secrets = sorted({s for t in texts for s in find_secrets(t)})
    if secrets:
        return Screening("reject", original, tuple(f"secret:{s}" for s in secrets))
    external = isinstance(item.get("provenance"), Mapping) and item["provenance"].get("source") == "external_content"
    signals = sorted({s for t in texts for s in instruction_signals(t)})
    if signals and external:
        return Screening("reject", original, tuple(f"external_instruction:{s}" for s in signals))

    out = copy.deepcopy(dict(item))
    reasons: list[str] = []
    content = out.get("content")
    pii: set[str] = set()
    if isinstance(content, dict):
        for key in TEXT_FIELDS:
            if isinstance(content.get(key), str):
                content[key], found = redact_pii(content[key])
                pii.update(found)
    if pii:
        out["sensitivity"] = "pii"
        reasons.extend(f"pii:{p}" for p in sorted(pii))
    if external:
        out["trust"] = min(float(out.get("trust", 0.0)), EXTERNAL_TRUST_CAP)
        reasons.append("external_low_trust")
    if signals:
        return Screening("review", out, (*reasons, *(f"instruction:{s}" for s in signals)))
    return Screening("accept", out, tuple(reasons))
