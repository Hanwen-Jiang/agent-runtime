"""命令行：python -m agentruntime_learning.pipeline run --tenant <租户> --family <任务族> --traces <文件>…（T-16）。

环境变量：AR_REGISTRY_ADDRESS（Registry 的内部端口，不是 dry-run 时必填）；AR_SECRETS_FILE（开发 SecretStore，平台作用域里的
registry/publish-token 是发布令牌）；AR_MODEL_GATEWAY_ADDRESS（可选：失败归因经 Model Gateway）；OTEL_EXPORTER_OTLP_ENDPOINT（可选：审计与指标发给 Collector）。
最后一行输出运行摘要的 JSON。退出码：0 跑完（已发布、发布被拒、没有候选、dry-run）；1 Registry 写失败；2 参数或环境不对。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from agentruntime_learning.pipeline.run import PipelineConfig, run_pipeline, summary_line
from agentruntime_learning.pipeline.traces import FORMATS, TraceFormatError

ENV_REGISTRY_ADDRESS = "AR_REGISTRY_ADDRESS"
ENV_SECRETS_FILE = "AR_SECRETS_FILE"
ENV_MODEL_GATEWAY_ADDRESS = "AR_MODEL_GATEWAY_ADDRESS"
PUBLISH_TOKEN = "registry/publish-token"
TENANT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
FAMILY = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
FAILED = ("publish_failed", "promote_failed")


class UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:  # 不直接退出：main 统一返回 2
        raise UsageError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="python -m agentruntime_learning.pipeline", description="Learning Pipeline：从 trace 归纳候选 Recipe，过评测门槛才经 Registry 发布")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="跑一遍闭环")
    run.add_argument("--tenant", required=True, help="租户（只读这个租户的 trace，Recipe 发布在它的作用域）")
    run.add_argument("--family", required=True, help="任务族，形如 finance.invoice_coding")
    run.add_argument("--traces", required=True, nargs="+", help="trace 文件（每行一条记录）")
    run.add_argument("--format", default="otlp_json", choices=FORMATS)
    run.add_argument("--report-dir", default="learning-reports")
    run.add_argument("--budget-usd", type=float, default=0.5, help="这次运行的模型调用预算（美元）；0 表示不调用模型")
    run.add_argument("--min-support", type=int, default=5)
    run.add_argument("--min-samples", type=int, default=100)
    run.add_argument("--delta", type=float, default=0.02)
    run.add_argument("--epsilon", type=float, default=0.05)
    run.add_argument("--dry-run", action="store_true", help="只出报告，不写 Registry")
    return parser


def _publish_token(env: Mapping[str, str]) -> str | None:
    path = env.get(ENV_SECRETS_FILE, "").strip()
    if not path:
        return None
    from agentruntime_core.dev.secret_store import LocalFileSecretStoreProvider
    from agentruntime_core.errors import NotFoundError

    async def read() -> str | None:
        try:
            secret = await LocalFileSecretStoreProvider(Path(path)).bind_platform("learning").read(PUBLISH_TOKEN)
        except NotFoundError:
            return None
        value = secret.reveal()
        return value.decode() if isinstance(value, bytes) else str(value)

    return asyncio.run(read())


async def _run(args: argparse.Namespace, env: Mapping[str, str], lines: list[str]) -> int:
    from agentruntime_core.adapters.otel import telemetry_from_env

    registry: Any = None
    gateway: Any = None
    telemetry = telemetry_from_env("learning", env)
    try:
        if not args.dry_run:
            from agentruntime_worker.registry import GrpcRegistry

            registry = GrpcRegistry(env[ENV_REGISTRY_ADDRESS].strip(), publish_token=args.publish_token)
        else:
            registry = _ReadOnlyOrNone(env.get(ENV_REGISTRY_ADDRESS, "").strip())
        if args.budget_usd > 0 and env.get(ENV_MODEL_GATEWAY_ADDRESS, "").strip():
            from agentruntime_worker.executor.grpc_gateway import from_env

            gateway = from_env(env)
        config = PipelineConfig(
            tenant_id=args.tenant,
            task_family=args.family,
            report_dir=Path(args.report_dir),
            min_support=args.min_support,
            min_samples=args.min_samples,
            delta=args.delta,
            epsilon=args.epsilon,
            budget_usd=args.budget_usd,
            dry_run=args.dry_run,
        )
        report = await run_pipeline(config, lines=lines, fmt=args.format, registry=registry, gateway=gateway, telemetry=telemetry, sources=list(args.traces))
        print(summary_line(report))
        return 1 if report.status in FAILED else 0
    finally:
        for client in (registry, gateway):
            if client is not None and hasattr(client, "aclose"):
                await client.aclose()
        await telemetry.aclose()


class _ReadOnlyOrNone:
    """dry-run：有 Registry 地址时只读（查模板与默认 Recipe），没有时模板一律按查不到、没有默认 Recipe；写方法一律不可用。"""

    def __init__(self, address: str) -> None:
        self._client: Any = None
        if address:
            from agentruntime_worker.registry import GrpcRegistry

            self._client = GrpcRegistry(address)

    async def get_node_template(self, tenant: Any, name: str, version: str = "") -> Any:
        if self._client is None:
            from agentruntime.common.v1 import enums_pb2
            from agentruntime.registry.v1 import registry_pb2 as rg

            # 没有 Registry 时按最保守的 effect 估 risk，只用于 dry-run 的报告。
            return rg.NodeTemplate(name=name, version=version, effect=enums_pb2.EFFECT_WRITE_IRREVERSIBLE)
        return await self._client.get_node_template(tenant, name, version)

    async def list_recipes(self, tenant: Any, task_family: str = "") -> list[Any]:
        return [] if self._client is None else await self._client.list_recipes(tenant, task_family)

    async def publish(self, *args: Any, **kw: Any) -> Any:
        raise AssertionError("dry-run 不写 Registry")

    async def promote(self, *args: Any, **kw: Any) -> Any:
        raise AssertionError("dry-run 不写 Registry")

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    try:
        args = _parser().parse_args(argv)
        if not TENANT.fullmatch(args.tenant):
            raise UsageError(f"租户 {args.tenant!r} 不合法")
        if not FAMILY.fullmatch(args.family):
            raise UsageError(f"任务族 {args.family!r} 不合法：形如 finance.invoice_coding")
        if args.budget_usd < 0:
            raise UsageError("--budget-usd 不能为负")
        if not args.dry_run and not env.get(ENV_REGISTRY_ADDRESS, "").strip():
            raise UsageError(f"没有设置 {ENV_REGISTRY_ADDRESS}（Registry 的 host:port）；只看结果可以加 --dry-run")
        lines: list[str] = []
        for name in args.traces:
            path = Path(name)
            if not path.is_file():
                raise UsageError(f"trace 文件不存在：{name}")
            lines.extend(path.read_text("utf-8").splitlines())
        args.publish_token = None if args.dry_run else _publish_token(env)
    except UsageError as err:
        print(f"learning pipeline：{err}", file=sys.stderr)
        return 2
    try:
        return asyncio.run(_run(args, env, lines))
    except TraceFormatError as err:
        print(f"learning pipeline：{err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
