"""Worker 进程的配置，从环境变量读取。

工作流代码不读这里：工作流必须确定，超时等参数由 Stage 定义（Node 的 budget.timeout_s）推出，
见 workflows/timeouts.py。这里只有进程级的设置与 S-03 桩的参数。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from agentruntime_core.adapters.s3_object_store import S3Settings

ENV_TEMPORAL_ADDRESS = "AR_TEMPORAL_ADDRESS"
ENV_TEMPORAL_NAMESPACE = "AR_TEMPORAL_NAMESPACE"
ENV_STUB_CALL_LOG = "AR_STUB_CALL_LOG"
ENV_STUB_GATE_DIR = "AR_STUB_GATE_DIR"
ENV_STUB_NODE_TIMEOUT_S = "AR_STUB_NODE_TIMEOUT_S"
ENV_DATA_DIR = "AR_DATA_DIR"
# Claim-Check（S-06）：ObjectStore 与 SecretStore 开发实现的位置、阈值。变量名与 task-service 相同。
ENV_OBJECT_STORE_DIR = "AR_OBJECT_STORE_DIR"
ENV_SECRETS_FILE = "AR_SECRETS_FILE"
# 集群里密钥文件放在共用的可写卷上，静态密钥来自只读的种子文件（K8s Secret），启动时并进来（D-04；与 task-service 相同）。
ENV_SECRETS_SEED_FILE = "AR_SECRETS_SEED_FILE"
ENV_CLAIM_CHECK_THRESHOLD = "AR_CLAIM_CHECK_THRESHOLD_BYTES"
# Planner（S-09）：stub 用 S-03 的桩 Planner（默认，端到端测试用它）；model 用真正的 Planner（经 Model Gateway）。
ENV_PLANNER = "AR_PLANNER"
PLANNERS = ("stub", "model")
# Reviewer 与收尾写回（S-10）：stub 用 S-03 的桩（默认）；model 用真正的 Reviewer（经 Model Gateway）与收尾写回。
ENV_REVIEWER = "AR_REVIEWER"
# 交付的快速检查（docs/plan/feedback-2026-11-01.md N-09）：空为不检查，jev 为 TypeSafe 的 Jev（密钥在 SecretStore 平台作用域的 judge/jev-api-key）。
ENV_JUDGE = "AR_JUDGE"
ENV_JUDGE_URL = "AR_JUDGE_URL"
ENV_JUDGE_MODEL = "AR_JUDGE_MODEL"
JUDGES = ("", "jev")
REVIEWERS = ("stub", "model")
# tool_loop 的一步：stub 用桩（默认，第一步就结束）；model 经 Model Gateway，上下文由 Context Builder 组装。
ENV_TOOL_LOOP = "AR_TOOL_LOOP"
TOOL_LOOPS = ("stub", "model")
# Context Builder 的阈值基准：模型实测的有效长度（token）。Model Profile 接好之前用这个配置（附录 G.2、G.5）。
ENV_CONTEXT_EFFECTIVE_TOKENS = "AR_CONTEXT_EFFECTIVE_TOKENS"
ENV_CONTEXT_OFFLOAD_TOKENS = "AR_CONTEXT_OFFLOAD_TOKENS"
# Model Gateway 的地址（host:port，gRPC）：设了时 Node Executor、Planner、Reviewer 的模型调用都经 GrpcModelGateway；
# 没设时用桩模型 StubModelGateway（端到端测试的默认）。
ENV_MODEL_GATEWAY_ADDRESS = "AR_MODEL_GATEWAY_ADDRESS"
# Registry 的地址（task-service 的内部端口，T-08）；设了才让 resolve_catalog 与 Planner 的上下文改读 Registry。
ENV_REGISTRY_ADDRESS = "AR_REGISTRY_ADDRESS"
ENV_MEMORY_ADDRESS = "AR_MEMORY_ADDRESS"
ENV_DEV_SIGNING_KEY = "AR_DEV_SIGNING_KEY"
# 平台内工具的工作区根、web_fetch 的主机名单（逗号分隔）、MCP 登记文件（T-10）；没设的那一类工具不登记。
ENV_TOOL_WORKSPACE_ROOT = "AR_TOOL_WORKSPACE_ROOT"
ENV_TOOL_FETCH_HOSTS = "AR_TOOL_FETCH_HOSTS"
ENV_MCP_TOOLS_FILE = "AR_MCP_TOOLS_FILE"
# shell_run 在哪里跑：host（默认，开发用，不隔离）、docker（一次性容器，不联网、限资源；coding agent MVP）或 sandbox
# （经 Sandbox 接口，后端登记在 AR_SANDBOX_BACKENDS_FILE，按租户策略选；docs/plan/cloud-deploy.md D-05）；docker 时容器的镜像。
ENV_TOOL_SHELL_SANDBOX = "AR_TOOL_SHELL_SANDBOX"
ENV_TOOL_SHELL_IMAGE = "AR_TOOL_SHELL_IMAGE"
ENV_SANDBOX_BACKENDS_FILE = "AR_SANDBOX_BACKENDS_FILE"
SHELL_SANDBOXES = ("host", "docker", "sandbox")
# 业务库（沙箱环境表在这里，D-11）：设了是 Postgres（行级安全，与 task-service 同一个库），没设是数据目录下的 SQLite。
ENV_RELATIONAL_DSN = "AR_RELATIONAL_DSN"
# 会话级沙箱（D-13）：sandbox 时默认按（工作区，会话）复用环境；设 0 改回每条命令一个环境。
ENV_SANDBOX_SESSIONS = "AR_SANDBOX_SESSIONS"
# 沙箱环境表的对账间隔（秒，D-11）。
ENV_SANDBOX_RECONCILE_INTERVAL_S = "AR_SANDBOX_RECONCILE_INTERVAL_S"
# 会话级环境在它的 Task 还没结束（等人）时最多保留多久（秒，从最后一次使用算起；docs/plan/feedback-2026-11-01.md N-05）。0 不保留。
ENV_SANDBOX_HOLD_MAX_S = "AR_SANDBOX_HOLD_MAX_S"
# shell_run 在沙箱里用的运行环境（D-14）：后端登记把它映射到镜像，例如 universal（多语言，默认的生产配置）或 python。
ENV_SANDBOX_RUNTIME = "AR_SANDBOX_RUNTIME"
# 包镜像的地址（D-15），例如 http://ar-agent-runtime-pkg-mirror:8093：设了就给 K8s 与本地后端的命令注入镜像源变量。
ENV_PKG_MIRROR_URL = "AR_PKG_MIRROR_URL"
# 出网网关的代理地址（D-16），例如 http://ar-agent-runtime-egress-gateway:8095：设了就给 K8s 与本地后端的命令注入代理变量，
# 并登记 request_network 工具；预览地址的域名（例如 preview.example.com），设了 request_network 才能开预览地址。
ENV_EGRESS_GATEWAY_URL = "AR_EGRESS_GATEWAY_URL"
ENV_PREVIEW_DOMAIN = "AR_PREVIEW_DOMAIN"
# Workspace API 收本地的远端（file://），只给测试与开发用（D-06）；生产只收 https。
ENV_WORKSPACE_ALLOW_LOCAL_REMOTES = "AR_WORKSPACE_ALLOW_LOCAL_REMOTES"


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
    # 启动时并进 secrets_file 的种子文件（AR_SECRETS_SEED_FILE）；None 表示不并。
    secrets_seed_file: Path | None = None
    # S3 兼容的对象存储（AR_S3_*，见 agentruntime_core.adapters.s3_object_store；D-09）：设了就不用 object_store_dir。
    object_store_s3: S3Settings | None = None
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
    # 工具的输出超过这么多 token 就转存，只留摘录（Context Builder 的 offload）。
    context_offload_tokens: int = 2_000
    # Model Gateway 的地址（AR_MODEL_GATEWAY_ADDRESS）；空表示用桩模型。
    model_gateway_address: str = ""
    # Registry 的地址（AR_REGISTRY_ADDRESS，T-08）；空表示 Node Catalog 与 Recipe 仍用桩。
    registry_address: str = ""
    # Memory Service 的地址（AR_MEMORY_ADDRESS）与签它的令牌用的密钥（AR_DEV_SIGNING_KEY）；地址为空时不接（docs/plan/memory-live.md）。
    memory_address: str = ""
    dev_signing_key: str = field(default="", repr=False)
    # 平台内工具的工作区根（AR_TOOL_WORKSPACE_ROOT）、web_fetch 的主机名单（AR_TOOL_FETCH_HOSTS）、MCP 登记文件（AR_MCP_TOOLS_FILE），T-10。
    tool_workspace_root: Path | None = None
    tool_fetch_hosts: tuple[str, ...] = ()
    mcp_tools_file: Path | None = None
    # shell_run 的沙箱（AR_TOOL_SHELL_SANDBOX）、docker 时的镜像（AR_TOOL_SHELL_IMAGE）、sandbox 时的后端登记文件（AR_SANDBOX_BACKENDS_FILE）。
    tool_shell_sandbox: str = "host"
    tool_shell_image: str = "python:3.13-slim"
    sandbox_backends_file: Path | None = None
    # 业务库的连接串（AR_RELATIONAL_DSN）；里面有口令，不进 repr 与比较。
    relational_dsn: str = field(default="", repr=False, compare=False)
    sandbox_sessions: bool = True
    sandbox_reconcile_interval_s: int = 60
    sandbox_hold_max_s: int = 7200
    sandbox_runtime: str = "python"
    pkg_mirror_url: str = ""
    egress_gateway_url: str = ""
    preview_domain: str = ""
    workspace_allow_local_remotes: bool = False
    judge: str = ""
    judge_url: str = "https://api.typesafe.ai"
    judge_model: str = "jev-latest"

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
        judge = env.get(ENV_JUDGE, "").strip()
        if judge not in JUDGES:
            raise ValueError(f"{ENV_JUDGE} 只能是空或 jev")
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
        offload = cls.context_offload_tokens
        raw = env.get(ENV_CONTEXT_OFFLOAD_TOKENS, "")
        if raw:
            if not raw.isdigit() or int(raw) <= 0:
                raise ValueError(f"{ENV_CONTEXT_OFFLOAD_TOKENS} 必须是正整数（token）")
            offload = int(raw)
        call_log = env.get(ENV_STUB_CALL_LOG, "")
        gate_dir = env.get(ENV_STUB_GATE_DIR, "")
        data_dir = Path(env[ENV_DATA_DIR]) if env.get(ENV_DATA_DIR) else None
        objects = env.get(ENV_OBJECT_STORE_DIR, "")
        secrets = env.get(ENV_SECRETS_FILE, "")
        workspace_root = env.get(ENV_TOOL_WORKSPACE_ROOT, "").strip()
        fetch_hosts = tuple(h.strip() for h in env.get(ENV_TOOL_FETCH_HOSTS, "").split(",") if h.strip())
        mcp_tools = env.get(ENV_MCP_TOOLS_FILE, "").strip()
        sandbox = env.get(ENV_TOOL_SHELL_SANDBOX, "").strip() or cls.tool_shell_sandbox
        if sandbox not in SHELL_SANDBOXES:
            raise ValueError(f"{ENV_TOOL_SHELL_SANDBOX} 只能是 {'、'.join(SHELL_SANDBOXES)}")
        backends = env.get(ENV_SANDBOX_BACKENDS_FILE, "").strip()
        if sandbox == "sandbox" and not backends:
            raise ValueError(f"{ENV_TOOL_SHELL_SANDBOX}=sandbox 时要给 {ENV_SANDBOX_BACKENDS_FILE}")
        sessions = env.get(ENV_SANDBOX_SESSIONS, "").strip() or "1"
        if sessions not in ("0", "1"):
            raise ValueError(f"{ENV_SANDBOX_SESSIONS} 只能是 0 或 1")
        hold_raw = env.get(ENV_SANDBOX_HOLD_MAX_S, "").strip()
        if hold_raw and (not hold_raw.isdigit()):
            raise ValueError(f"{ENV_SANDBOX_HOLD_MAX_S} 必须是不小于 0 的整数（秒）")
        hold_max = int(hold_raw) if hold_raw else cls.sandbox_hold_max_s
        interval = cls.sandbox_reconcile_interval_s
        raw = env.get(ENV_SANDBOX_RECONCILE_INTERVAL_S, "")
        if raw:
            if not raw.isdigit() or int(raw) < 5:
                raise ValueError(f"{ENV_SANDBOX_RECONCILE_INTERVAL_S} 必须是不小于 5 的整数（秒）")
            interval = int(raw)
        local_remotes = env.get(ENV_WORKSPACE_ALLOW_LOCAL_REMOTES, "").strip() or "0"
        if local_remotes not in ("0", "1"):
            raise ValueError(f"{ENV_WORKSPACE_ALLOW_LOCAL_REMOTES} 只能是 0 或 1")
        seed = env.get(ENV_SECRETS_SEED_FILE, "").strip()
        if seed and not secrets:
            raise ValueError(f"设了 {ENV_SECRETS_SEED_FILE} 还要设 {ENV_SECRETS_FILE}（种子并进哪个文件）")
        return cls(
            temporal_address=env.get(ENV_TEMPORAL_ADDRESS, "") or cls.temporal_address,
            namespace=env.get(ENV_TEMPORAL_NAMESPACE, "") or cls.namespace,
            call_log=Path(call_log) if call_log else None,
            gate_dir=Path(gate_dir) if gate_dir else None,
            stub_node_timeout_s=timeout,
            data_dir=data_dir,
            object_store_dir=Path(objects) if objects else (data_dir / "objects" if data_dir else None),
            secrets_file=Path(secrets) if secrets else (data_dir / "secrets.json" if data_dir else None),
            secrets_seed_file=Path(seed) if seed else None,
            object_store_s3=S3Settings.from_env(env),
            claim_check_threshold=threshold,
            planner=planner,
            reviewer=reviewer,
            judge=judge,
            judge_url=env.get(ENV_JUDGE_URL, "").strip() or cls.judge_url,
            judge_model=env.get(ENV_JUDGE_MODEL, "").strip() or cls.judge_model,
            tool_loop=tool_loop,
            context_effective_tokens=effective,
            context_offload_tokens=offload,
            model_gateway_address=env.get(ENV_MODEL_GATEWAY_ADDRESS, "").strip(),
            registry_address=env.get(ENV_REGISTRY_ADDRESS, "").strip(),
            memory_address=env.get(ENV_MEMORY_ADDRESS, "").strip(),
            dev_signing_key=env.get(ENV_DEV_SIGNING_KEY, ""),
            tool_workspace_root=Path(workspace_root) if workspace_root else None,
            tool_fetch_hosts=fetch_hosts,
            mcp_tools_file=Path(mcp_tools) if mcp_tools else None,
            tool_shell_sandbox=sandbox,
            tool_shell_image=env.get(ENV_TOOL_SHELL_IMAGE, "").strip() or cls.tool_shell_image,
            sandbox_backends_file=Path(backends) if backends else None,
            relational_dsn=env.get(ENV_RELATIONAL_DSN, "").strip(),
            sandbox_sessions=sessions == "1",
            sandbox_reconcile_interval_s=interval,
            sandbox_hold_max_s=hold_max,
            sandbox_runtime=env.get(ENV_SANDBOX_RUNTIME, "").strip() or cls.sandbox_runtime,
            pkg_mirror_url=env.get(ENV_PKG_MIRROR_URL, "").strip(),
            egress_gateway_url=env.get(ENV_EGRESS_GATEWAY_URL, "").strip(),
            preview_domain=env.get(ENV_PREVIEW_DOMAIN, "").strip(),
            workspace_allow_local_remotes=local_remotes == "1",
        )

    def store_paths(self, root: Path) -> tuple[Path, Path]:
        """ObjectStore 目录与密钥文件：配置了就用配置的，否则放在 root 下（与 task-service 的默认布局相同）。"""
        return self.object_store_dir or root / "objects", self.secrets_file or root / "secrets.json"
