"""Worker 进程的配置，从环境变量读取。

工作流代码不读这里：工作流必须确定，超时等参数由 Stage 定义（Node 的 budget.timeout_s）推出，
见 workflows/timeouts.py。这里只有进程级的设置与 S-03 桩的参数。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

ENV_TEMPORAL_ADDRESS = "AR_TEMPORAL_ADDRESS"
ENV_TEMPORAL_NAMESPACE = "AR_TEMPORAL_NAMESPACE"
ENV_STUB_CALL_LOG = "AR_STUB_CALL_LOG"
ENV_STUB_GATE_DIR = "AR_STUB_GATE_DIR"
ENV_STUB_NODE_TIMEOUT_S = "AR_STUB_NODE_TIMEOUT_S"
ENV_DATA_DIR = "AR_DATA_DIR"
# Claim-Check（S-06）：ObjectStore 与 SecretStore 开发实现的位置、阈值。变量名与 task-service 相同。
ENV_OBJECT_STORE_DIR = "AR_OBJECT_STORE_DIR"
ENV_SECRETS_FILE = "AR_SECRETS_FILE"
ENV_CLAIM_CHECK_THRESHOLD = "AR_CLAIM_CHECK_THRESHOLD_BYTES"
# Planner（S-09）：stub 用 S-03 的桩 Planner（默认，端到端测试用它）；model 用真正的 Planner（经 Model Gateway）。
ENV_PLANNER = "AR_PLANNER"
PLANNERS = ("stub", "model")
# Reviewer 与收尾写回（S-10）：stub 用 S-03 的桩（默认）；model 用真正的 Reviewer（经 Model Gateway）与收尾写回。
ENV_REVIEWER = "AR_REVIEWER"
REVIEWERS = ("stub", "model")
# tool_loop 的一步：stub 用桩（默认，第一步就结束）；model 经 Model Gateway，上下文由 Context Builder 组装。
ENV_TOOL_LOOP = "AR_TOOL_LOOP"
TOOL_LOOPS = ("stub", "model")
# Context Builder 的阈值基准：模型实测的有效长度（token）。Model Profile 接好之前用这个配置（附录 G.2、G.5）。
ENV_CONTEXT_EFFECTIVE_TOKENS = "AR_CONTEXT_EFFECTIVE_TOKENS"
# Model Gateway 的地址（host:port，gRPC）：设了时 Node Executor、Planner、Reviewer 的模型调用都经 GrpcModelGateway；
# 没设时用桩模型 StubModelGateway（端到端测试的默认）。
ENV_MODEL_GATEWAY_ADDRESS = "AR_MODEL_GATEWAY_ADDRESS"


@dataclass(frozen=True)
class WorkerConfig:
    temporal_address: str = "localhost:7233"
    namespace: str = "default"
    # 桩 activity 的调用记录（JSON Lines），测试据此断言调用次数；None 表示不记。
    call_log: Path | None = None
    # slow_answer 桩等待 <gate_dir>/<task_id> 出现后才返回。
    gate_dir: Path | None = None
    # 桩 Planner 给 model 节点的 budget.timeout_s。
    stub_node_timeout_s: int = 600
    # 开发实现（ObjectStore、SecretStore、Sandbox）的数据目录；None 时用临时目录。
    data_dir: Path | None = None
    # ObjectStore 与 SecretStore 开发实现的位置；与 task-service 指向同一处，两边才能互相解开对方卸载的载荷。
    object_store_dir: Path | None = None
    secrets_file: Path | None = None
    # Claim-Check 的阈值（字节）。
    claim_check_threshold: int = 64 * 1024
    # 哪个 Planner：stub 或 model（S-09）。
    planner: str = "stub"
    # 哪个 Reviewer：stub 或 model（S-10）。
    reviewer: str = "stub"
    # 哪个 tool_loop：stub 或 model（Context Builder）。
    tool_loop: str = "stub"
    # 模型实测的有效长度（token），Context Builder 的阈值按它算。
    context_effective_tokens: int = 32_000
    # Model Gateway 的地址（AR_MODEL_GATEWAY_ADDRESS）；空表示用桩模型。
    model_gateway_address: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> WorkerConfig:
        """读取并校验；取值不合法时抛 ValueError。"""
        timeout = cls.stub_node_timeout_s
        raw = env.get(ENV_STUB_NODE_TIMEOUT_S, "")
        if raw:
            if not raw.isdigit() or int(raw) <= 0:
                raise ValueError(f"{ENV_STUB_NODE_TIMEOUT_S} 必须是正整数（秒）")
            timeout = int(raw)
        threshold = cls.claim_check_threshold
        raw = env.get(ENV_CLAIM_CHECK_THRESHOLD, "")
        if raw:
            if not raw.isdigit() or int(raw) <= 0:
                raise ValueError(f"{ENV_CLAIM_CHECK_THRESHOLD} 必须是正整数（字节）")
            threshold = int(raw)
        planner = env.get(ENV_PLANNER, "") or cls.planner
        if planner not in PLANNERS:
            raise ValueError(f"{ENV_PLANNER} 只能是 {'、'.join(PLANNERS)}")
        reviewer = env.get(ENV_REVIEWER, "") or cls.reviewer
        if reviewer not in REVIEWERS:
            raise ValueError(f"{ENV_REVIEWER} 只能是 {'、'.join(REVIEWERS)}")
        tool_loop = env.get(ENV_TOOL_LOOP, "") or cls.tool_loop
        if tool_loop not in TOOL_LOOPS:
            raise ValueError(f"{ENV_TOOL_LOOP} 只能是 {'、'.join(TOOL_LOOPS)}")
        effective = cls.context_effective_tokens
        raw = env.get(ENV_CONTEXT_EFFECTIVE_TOKENS, "")
        if raw:
            if not raw.isdigit() or int(raw) <= 0:
                raise ValueError(f"{ENV_CONTEXT_EFFECTIVE_TOKENS} 必须是正整数（token）")
            effective = int(raw)
        call_log = env.get(ENV_STUB_CALL_LOG, "")
        gate_dir = env.get(ENV_STUB_GATE_DIR, "")
        data_dir = Path(env[ENV_DATA_DIR]) if env.get(ENV_DATA_DIR) else None
        objects = env.get(ENV_OBJECT_STORE_DIR, "")
        secrets = env.get(ENV_SECRETS_FILE, "")
        return cls(
            temporal_address=env.get(ENV_TEMPORAL_ADDRESS, "") or cls.temporal_address,
            namespace=env.get(ENV_TEMPORAL_NAMESPACE, "") or cls.namespace,
            call_log=Path(call_log) if call_log else None,
            gate_dir=Path(gate_dir) if gate_dir else None,
            stub_node_timeout_s=timeout,
            data_dir=data_dir,
            object_store_dir=Path(objects) if objects else (data_dir / "objects" if data_dir else None),
            secrets_file=Path(secrets) if secrets else (data_dir / "secrets.json" if data_dir else None),
            claim_check_threshold=threshold,
            planner=planner,
            reviewer=reviewer,
            tool_loop=tool_loop,
            context_effective_tokens=effective,
            model_gateway_address=env.get(ENV_MODEL_GATEWAY_ADDRESS, "").strip(),
        )

    def store_paths(self, root: Path) -> tuple[Path, Path]:
        """ObjectStore 目录与密钥文件：配置了就用配置的，否则放在 root 下（与 task-service 的默认布局相同）。"""
        return self.object_store_dir or root / "objects", self.secrets_file or root / "secrets.json"
