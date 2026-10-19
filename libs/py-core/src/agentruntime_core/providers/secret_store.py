"""SecretStore（设计书 4.12）：读取密钥、按租户签发短期动态凭据、吊销，以及按租户的数据密钥。

两种语言共用：Go 侧见 libs/go-core/providers/secretstore，方法一一对应，语义一致。

节点代码永远拿不到长期密钥：program 节点只拿 issue_credential 签发的短期凭据。所有明文都包在
Secret 里，打印、格式化、序列化都只得到掩码；错误信息里不出现任何明文。

语义：
- read：读取本作用域的静态密钥。平台作用域是基础设施密钥，租户作用域是该租户的密钥（例如 BYOK）。
  不存在或属于别的作用域：not_found。
- issue_credential：按角色签发短期凭据。角色没有授予本作用域：denied。ttl 必须大于 0，且不超过实现配置的上限，否则 invalid。
- lease_status：ACTIVE、REVOKED 或 EXPIRED。租约不存在或属于别的作用域：not_found。
- revoke：吊销后立即变为 REVOKED；重复吊销不报错。
- generate_data_key：生成 32 字节的数据密钥，返回明文与包装后的密文（信封加密）。供 Claim-Check（S-06）与向量加密（S-12b）使用。
- unwrap_data_key：解包。密文属于别的作用域，或 purpose 与生成时不同：denied；密文损坏：invalid。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from agentruntime_core.providers.base import Bindable
from agentruntime_core.secrets import Secret


class LeaseStatus(enum.StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


@dataclass(frozen=True)
class Credential:
    lease_id: str
    role: str
    secret: Secret
    expires_at: datetime


@dataclass(frozen=True)
class DataKey:
    key_id: str
    plaintext: Secret
    wrapped: bytes


class SecretStore(Protocol):
    async def read(self, name: str) -> Secret: ...

    async def issue_credential(self, role: str, *, ttl: timedelta) -> Credential: ...

    async def lease_status(self, lease_id: str) -> LeaseStatus: ...

    async def revoke(self, lease_id: str) -> None: ...

    async def generate_data_key(self, purpose: str) -> DataKey: ...

    async def unwrap_data_key(self, wrapped: bytes, *, purpose: str) -> Secret: ...


class SecretStoreProvider(Bindable[SecretStore], Protocol):
    pass
