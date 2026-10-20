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

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> WorkerConfig:
        """读取并校验；取值不合法时抛 ValueError。"""
        timeout = cls.stub_node_timeout_s
        raw = env.get(ENV_STUB_NODE_TIMEOUT_S, "")
        if raw:
            if not raw.isdigit() or int(raw) <= 0:
                raise ValueError(f"{ENV_STUB_NODE_TIMEOUT_S} 必须是正整数（秒）")
            timeout = int(raw)
        call_log = env.get(ENV_STUB_CALL_LOG, "")
        gate_dir = env.get(ENV_STUB_GATE_DIR, "")
        data_dir = env.get(ENV_DATA_DIR, "")
        return cls(
            temporal_address=env.get(ENV_TEMPORAL_ADDRESS, "") or cls.temporal_address,
            namespace=env.get(ENV_TEMPORAL_NAMESPACE, "") or cls.namespace,
            call_log=Path(call_log) if call_log else None,
            gate_dir=Path(gate_dir) if gate_dir else None,
            stub_node_timeout_s=timeout,
            data_dir=Path(data_dir) if data_dir else None,
        )
