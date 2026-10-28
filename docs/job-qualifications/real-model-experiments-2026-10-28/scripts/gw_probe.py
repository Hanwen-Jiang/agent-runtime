"""对网关发一次最小的 Generate，看它多久回、回什么。用法：gw_probe.py <host:port> <tier>"""

import asyncio
import sys
import time

from agentruntime.common.v1 import enums_pb2, tenant_pb2
from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg

from agentruntime_worker.executor.grpc_gateway import GrpcModelGateway

sys.stdout.reconfigure(encoding="utf-8")


async def main(address: str, tier: str) -> None:
    gw = GrpcModelGateway(address)
    req = mg.GenerateRequest(
        tenant=tenant_pb2.TenantContext(tenant_id="eval", user_id="probe"),
        call=mg.CallContext(tier=tier, role=enums_pb2.ROLE_JUDGE, max_cost_usd_micros=50_000),
        generation=mg.GenerateParams(messages=[mg.ChatMessage(role=mg.CHAT_ROLE_USER, text="reply with ok")]),
    )
    t = time.monotonic()
    try:
        resp = await asyncio.wait_for(gw.generate(req), timeout=float(sys.argv[3]) if len(sys.argv) > 3 else 90)
        print(f"ok in {time.monotonic() - t:.1f}s model={resp.model.model_profile_id} cost_micros={resp.usage.cost_usd_micros} text={resp.text[:40]!r}")
    except Exception as e:  # noqa: BLE001
        print(f"fail after {time.monotonic() - t:.1f}s: {type(e).__name__}: {str(e)[:300]}")
    finally:
        await gw.aclose()


asyncio.run(main(sys.argv[1], sys.argv[2]))
