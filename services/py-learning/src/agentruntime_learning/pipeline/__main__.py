"""命令行：python -m agentruntime_learning.pipeline run --tenant <租户> --family <任务族>（T-16），trace 二选一：
- --traces <文件>…：本地与离线跑；
- --topic <主题> --group <消费组>：集群里 KEDA 起的 Job 直接消费 Collector 写的主题（见 stream）。从最早保留的记录读到起跑时的末尾，
  跑完（退出码 0，不是 dry-run）才把末尾提交为消费组的位置；其他结局一律不提交，KEDA 下一轮再起 Job 整条重跑。

环境变量：AR_REGISTRY_ADDRESS（Registry 的内部端口，不是 dry-run 时必填）；AR_SECRETS_FILE（开发 SecretStore，平台作用域里的
registry/publish-token 是发布令牌）；AR_EVENT_STREAM_BROKERS（--topic 时必填：EventStream 的 host:port，逗号分隔）；
AR_MODEL_GATEWAY_ADDRESS（可选：失败归因经 Model Gateway）；OTEL_EXPORTER_OTLP_ENDPOINT（可选：审计与指标发给 Collector）。
成本（R-06-D）：--cost-source 默认 auto，配了业务库（AR_RELATIONAL_DSN 是 Postgres，否则 AR_DATA_DIR 下的 relational 是开发用的 SQLite，
与 Model Gateway 同一个）就读 Gateway 结算的 model_usage，否则读 trace 上的 runtime.cost_usd；model_usage 要求配了业务库，trace 照旧。
最后一行输出运行摘要的 JSON。退出码：0 跑完（已发布、发布被拒、没有候选、dry-run）；1 Registry 写失败；2 参数、环境或 trace 的格式不对；
3 读主题或提交消费组的位置失败；4 读不了业务库的 model_usage（不提交消费组的位置，不退回 trace 上的成本）。
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

from agentruntime_learning.pipeline import stream
from agentruntime_learning.pipeline.costs import CostSourceError, ledger_costs
from agentruntime_learning.pipeline.run import PipelineConfig, run_pipeline, summary_line
from agentruntime_learning.pipeline.traces import FORMATS, TraceFormatError

ENV_REGISTRY_ADDRESS = "AR_REGISTRY_ADDRESS"
ENV_SECRETS_FILE = "AR_SECRETS_FILE"
ENV_MODEL_GATEWAY_ADDRESS = "AR_MODEL_GATEWAY_ADDRESS"
ENV_RELATIONAL_DSN = "AR_RELATIONAL_DSN"
ENV_DATA_DIR = "AR_DATA_DIR"
COST_SOURCES = ("auto", "model_usage", "trace")
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
    source = run.add_mutually_exclusive_group(required=True)
    source.add_argument("--traces", nargs="+", help="trace 文件（每行一条记录）")
    source.add_argument("--topic", help=f"直接消费的 trace 主题（Collector 的 kafka 导出器写的，broker 取 {stream.ENV_BROKERS}）")
    run.add_argument("--group", help="配 --topic：KEDA 盯的消费组，跑完才提交它的位置")
    run.add_argument("--format", default="otlp_json", choices=FORMATS)
    run.add_argument("--report-dir", default="learning-reports")
    run.add_argument("--budget-usd", type=float, default=0.5, help="这次运行的模型调用预算（美元）；0 表示不调用模型")
    run.add_argument("--min-support", type=int, default=5)
    run.add_argument("--min-samples", type=int, default=100)
    run.add_argument("--delta", type=float, default=0.02)
    run.add_argument("--epsilon", type=float, default=0.05)
    run.add_argument("--dry-run", action="store_true", help="只出报告，不写 Registry")
    run.add_argument("--cost-source", default="auto", choices=COST_SOURCES,
                     help=f"Task 的成本读哪里：model_usage（Gateway 结算的计量表，要 {ENV_RELATIONAL_DSN} 或 {ENV_DATA_DIR}）、trace，auto 是配了业务库就读计量表")
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


async def _ledger(env: Mapping[str, str]) -> Any:
    """业务库：Postgres（AR_RELATIONAL_DSN）或开发用的 SQLite（AR_DATA_DIR/relational），与 Model Gateway 同一个。"""
    dsn = env.get(ENV_RELATIONAL_DSN, "").strip()
    if dsn:
        from agentruntime_core.adapters.postgres import PostgresRelationalStoreProvider
        from agentruntime_core.errors import ProviderError

        try:
            return await PostgresRelationalStoreProvider.connect(dsn, max_conns=1)
        except ProviderError as err:
            raise CostSourceError(f"连不上业务库：{err.code}") from None
    from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider

    return SQLiteRelationalStoreProvider(Path(env[ENV_DATA_DIR].strip()) / "relational")


async def _run(args: argparse.Namespace, env: Mapping[str, str], lines: list[str], sources: list[str]) -> int:
    from agentruntime_core.adapters.otel import telemetry_from_env

    registry: Any = None
    gateway: Any = None
    ledger: Any = None
    telemetry = telemetry_from_env("learning", env)
    try:
        costs = None
        if args.cost_source == "model_usage":
            ledger = await _ledger(env)

            async def costs(tenant_id: str, task_ids: list[str]) -> Any:
                return await ledger_costs(ledger, tenant_id, task_ids)

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
        report = await run_pipeline(config, lines=lines, fmt=args.format, registry=registry, gateway=gateway, telemetry=telemetry, sources=sources,
                                    costs=costs)
        print(summary_line(report))
        return 1 if report.status in FAILED else 0
    finally:
        for client in (registry, gateway, ledger):
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


def _check(args: argparse.Namespace, env: Mapping[str, str]) -> None:
    if not TENANT.fullmatch(args.tenant):
        raise UsageError(f"租户 {args.tenant!r} 不合法")
    if not FAMILY.fullmatch(args.family):
        raise UsageError(f"任务族 {args.family!r} 不合法：形如 finance.invoice_coding")
    if args.budget_usd < 0:
        raise UsageError("--budget-usd 不能为负")
    if not args.dry_run and not env.get(ENV_REGISTRY_ADDRESS, "").strip():
        raise UsageError(f"没有设置 {ENV_REGISTRY_ADDRESS}（Registry 的 host:port）；只看结果可以加 --dry-run")
    has_store = bool(env.get(ENV_RELATIONAL_DSN, "").strip() or env.get(ENV_DATA_DIR, "").strip())
    if args.cost_source == "auto":
        args.cost_source = "model_usage" if has_store else "trace"
    elif args.cost_source == "model_usage" and not has_store:
        raise UsageError(f"--cost-source model_usage 要设 {ENV_RELATIONAL_DSN}（Postgres）或 {ENV_DATA_DIR}（开发用的 SQLite），与 Model Gateway 同一个业务库")
    if args.topic is None:
        if args.group is not None:
            raise UsageError("--group 只配 --topic 用")
        return
    if args.group is None:
        raise UsageError("--topic 要配 --group（KEDA 盯的消费组，跑完才提交它的位置）")
    if not stream.NAME.fullmatch(args.topic):
        raise UsageError(f"主题 {args.topic!r} 不合法")
    if not stream.NAME.fullmatch(args.group):
        raise UsageError(f"消费组 {args.group!r} 不合法")
    if not env.get(stream.ENV_BROKERS, "").strip():
        raise UsageError(f"没有设置 {stream.ENV_BROKERS}（EventStream 的 host:port，逗号分隔）")


def _read_files(names: Sequence[str]) -> list[str]:
    lines: list[str] = []
    for name in names:
        path = Path(name)
        if not path.is_file():
            raise UsageError(f"trace 文件不存在：{name}")
        lines.extend(path.read_text("utf-8").splitlines())
    return lines


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    reader: Any = None
    snapshot: stream.Snapshot | None = None
    try:
        args = _parser().parse_args(argv)
        _check(args, env)
        args.publish_token = None if args.dry_run else _publish_token(env)
        if args.topic is None:
            lines, sources = _read_files(args.traces), list(args.traces)
        else:
            reader = stream.TopicReader(env[stream.ENV_BROKERS].strip(), args.topic, args.group)
            snapshot = reader.snapshot()
            lines, sources = snapshot.lines(), snapshot.sources
        code = asyncio.run(_run(args, env, lines, sources))
        # 只有跑完才提交：失败时 KEDA 看到的延迟不变，下一轮整条重跑。dry-run 不改消费组的位置。
        if code == 0 and snapshot is not None and not args.dry_run:
            reader.commit(snapshot)
        return code
    except UsageError as err:
        print(f"learning pipeline：{err}", file=sys.stderr)
        return 2
    except TraceFormatError as err:
        where = snapshot.where(err.line) if snapshot is not None else ""
        print(f"learning pipeline：{err}" + (f"（{where}）" if where else ""), file=sys.stderr)
        return 2
    except stream.StreamError as err:
        print(f"learning pipeline：{err}；没有提交消费组的位置", file=sys.stderr)
        return 3
    except CostSourceError as err:
        print(f"learning pipeline：{err}；没有提交消费组的位置", file=sys.stderr)
        return 4
    finally:
        if reader is not None:
            reader.close()


if __name__ == "__main__":
    sys.exit(main())
