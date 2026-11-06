"""Read the immutable, task-pinned personal package in activities only."""
from __future__ import annotations

import dataclasses
import hashlib
import json

from opentelemetry import trace
from agentruntime_core.errors import InvalidError
from agentruntime_core.tenancy import TenantContext

PREFERENCE_HEADER = "【个人指令】这是提交者的长期偏好；本次用户的明确要求优先，平台规则与权限边界始终有效。"


def preference(instructions: str) -> str:
    return f"{PREFERENCE_HEADER}\n{instructions}" if instructions else ""


@dataclasses.dataclass(frozen=True)
class PersonalConfig:
    version: str = ""
    digest: str = ""
    instructions: str = ""


class TaskPersonalConfig:
    def __init__(self, store):
        self.store = store

    async def load(self, tenant_id: str, task_id: str) -> PersonalConfig:
        if not tenant_id or not task_id:
            return PersonalConfig()
        async with self.store.bind(TenantContext(tenant_id=tenant_id)).transaction(read_only=True) as tx:
            row = await tx.query_one("SELECT * FROM tasks WHERE task_id = $1", (task_id,))
            if row is None or row.get("personal_config_pin", "") in ("", "none"):
                return PersonalConfig()
            try:
                pin = json.loads(row["personal_config_pin"])
                label = pin["version"]
                version = int(label.removeprefix("user/v"))
                if version <= 0 or label != f"user/v{version}":
                    raise ValueError
                digest = pin["digest"]
            except (ValueError, KeyError, TypeError):
                raise InvalidError("invalid_personal_config_pin", "固定的个人配置无效") from None
            entry = await tx.query_one("SELECT document FROM registry_entries WHERE kind = $1 AND name = $2 AND version = $3", (8, f"~{row['submitted_by']}/personal-config", version))
        if entry is None or hashlib.sha256(entry["document"].encode()).hexdigest() != digest:
            raise InvalidError("personal_config_digest_mismatch", "固定的个人配置不存在或校验失败")
        doc = json.loads(entry["document"])
        instructions = str(doc.get("instructions", ""))
        if len(instructions.encode()) > 4096:
            raise InvalidError("invalid_personal_config_pin", "个人指令超出限制")
        span = trace.get_current_span()
        span.set_attribute("runtime.personal_config.version", label)
        span.set_attribute("runtime.personal_config.digest", digest)
        return PersonalConfig(label, digest, instructions)


class PersonalConfigContextSource:
    def __init__(self, inner, personal: TaskPersonalConfig):
        self.inner, self.personal = inner, personal

    async def load(self, inp):
        ctx = await self.inner.load(inp)
        config = await self.personal.load(inp.tenant.tenant_id, inp.task_id)
        return dataclasses.replace(ctx, personal_instructions=config.instructions)
