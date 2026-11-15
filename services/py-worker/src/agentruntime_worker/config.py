"""Worker 进程的配置，从环境变量读取。

工作流代码不读这里：工作流必须确定，超时等参数由 Stage 定义（Node 的 budget.timeout_s）推出，
见 workflows/timeouts.py。这里只有进程级的设置与 S-03 桩的参数。
"""

from __future__ import annotations

import re
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
# Jev 的密钥文件（集群里挂载的 Secret）；不给时从 SecretStore 平台作用域的 judge/jev-api-key 读。
ENV_JUDGE_KEY_FILE = "AR_JUDGE_KEY_FILE"
JUDGES = ("", "jev")
REVIEWERS = ("stub", "model")
# tool_loop 的一步：stub 用桩（默认，第一步就结束）；model 经 Model Gateway，上下文由 Context Builder 组装。
ENV_TOOL_LOOP = "AR_TOOL_LOOP"
# N-19/N-20：没有输出 schema 的 deliver 节点经 StreamGenerate 生成并记首个 token 的用时（默认打开，0 关掉）。
ENV_STREAM_DELIVER = "AR_STREAM_DELIVER"
# DELTA（docs/plan/all-plans-dag-2026-11-03.md 7.1）：task-service 内部端口的地址（http://主机:端口），设了才把流式生成的片段
# 推成 token_delta、thinking_delta；令牌取 AR_SECRETS_FILE 平台作用域的 delta/<AR_DELTA_CALLER>（默认 worker）。
ENV_TASK_SERVICE_INTERNAL_URL = "AR_TASK_SERVICE_INTERNAL_URL"
ENV_DELTA_CALLER = "AR_DELTA_CALLER"
# N16-R（docs/plan/sub-agent.md 第 4～6 节）：rules 时 Planner 要 sub_agent 写 brief、接综合节点、按份额写预算，子 Agent 用 mid 档；
# 默认 off（第 10 节第 1 条：评测对照通过后再默认打开）。
ENV_SUB_AGENT_POLICY = "AR_SUB_AGENT_POLICY"
SUB_AGENT_POLICIES = ("off", "rules")
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
# Sandbox Service 的地址（SBX-01）：Worker 的沙箱一律经它（shell_run、repo_*、Workspace 操作、sandbox_exec）。
ENV_SANDBOX_SERVICE_ADDRESS = "AR_SANDBOX_SERVICE_ADDRESS"
SHELL_SANDBOXES = ("host", "docker", "sandbox")
# 业务库（沙箱环境表在这里，D-11）：设了是 Postgres（行级安全，与 task-service 同一个库），没设是数据目录下的 SQLite。
ENV_RELATIONAL_DSN = "AR_RELATIONAL_DSN"
# 会话级沙箱（D-13）：sandbox 时默认按（工作区，会话）复用环境；设 0 改回每条命令一个环境。
ENV_SANDBOX_SESSIONS = "AR_SANDBOX_SESSIONS"
# shell_run 在沙箱里用的运行环境（D-14）：后端登记把它映射到镜像，例如 universal（多语言，默认的生产配置）或 python。
ENV_SANDBOX_RUNTIME = "AR_SANDBOX_RUNTIME"
# 出网网关的代理地址（D-16）：设了就登记 request_network 工具（注入代理变量的是 Sandbox Service，SBX-01）；预览地址的域名（例如 preview.example.com），设了 request_network 才能开预览地址。
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
    # 没有输出 schema 的 deliver 节点经 StreamGenerate 生成（默认打开，AR_STREAM_DELIVER=0 关掉；网关客户端支持时）。
    stream_deliver: bool = True
    # 逐字增量推到哪里（AR_TASK_SERVICE_INTERNAL_URL）与调用方的名字（AR_DELTA_CALLER）；地址为空时不推。
    task_service_internal_url: str = ""
    delta_caller: str = "worker"
    # 子 Agent 的规矩（AR_SUB_AGENT_POLICY）：off 或 rules。
    sub_agent_policy: str = "off"
    # 部署配置的输入预算（token），Context Builder 按比例计算阈值；不是模型标称窗口或质量评测结论。
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
    # shell_run 的沙箱（AR_TOOL_SHELL_SANDBOX）、docker 时的镜像（AR_TOOL_SHELL_IMAGE）。sandbox 时经 Sandbox Service
    # （AR_SANDBOX_SERVICE_ADDRESS，SBX-01）；后端登记文件（AR_SANDBOX_BACKENDS_FILE）归服务，Worker 只看里面有没有托管后端
    # （有时登记 request_network）。
    tool_shell_sandbox: str = "host"
    tool_shell_image: str = "python:3.13-slim"
    sandbox_backends_file: Path | None = None
    sandbox_service_address: str = ""
    # 业务库的连接串（AR_RELATIONAL_DSN）；里面有口令，不进 repr 与比较。
    relational_dsn: str = field(default="", repr=False, compare=False)
    sandbox_sessions: bool = True
    sandbox_runtime: str = "python"
    egress_gateway_url: str = ""
    preview_domain: str = ""
    workspace_allow_local_remotes: bool = False
    judge: str = ""
    judge_url: str = "https://api.typesafe.ai"
    judge_model: str = "jev-latest"
    judge_key_file: Path | None = None

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
        service = env.get(ENV_SANDBOX_SERVICE_ADDRESS, "").strip()
        if sandbox == "sandbox" and not service:
            raise ValueError(f"{ENV_TOOL_SHELL_SANDBOX}=sandbox 时要给 {ENV_SANDBOX_SERVICE_ADDRESS}（沙箱一律经 Sandbox Service）")
        sessions = env.get(ENV_SANDBOX_SESSIONS, "").strip() or "1"
        if sessions not in ("0", "1"):
            raise ValueError(f"{ENV_SANDBOX_SESSIONS} 只能是 0 或 1")
        local_remotes = env.get(ENV_WORKSPACE_ALLOW_LOCAL_REMOTES, "").strip() or "0"
        if local_remotes not in ("0", "1"):
            raise ValueError(f"{ENV_WORKSPACE_ALLOW_LOCAL_REMOTES} 只能是 0 或 1")
        stream = env.get(ENV_STREAM_DELIVER, "").strip() or "1"
        if stream not in ("0", "1"):
            raise ValueError(f"{ENV_STREAM_DELIVER} 只能是 0 或 1")
        internal = env.get(ENV_TASK_SERVICE_INTERNAL_URL, "").strip().rstrip("/")
        if internal and not re.fullmatch(r"https?://[^/\s]+", internal):
            raise ValueError(f"{ENV_TASK_SERVICE_INTERNAL_URL} 要是 http(s)://主机:端口")
        sub_agents = env.get(ENV_SUB_AGENT_POLICY, "").strip() or cls.sub_agent_policy
        if sub_agents not in SUB_AGENT_POLICIES:
            raise ValueError(f"{ENV_SUB_AGENT_POLICY} 只能是 {'、'.join(SUB_AGENT_POLICIES)}")
        caller = env.get(ENV_DELTA_CALLER, "").strip() or cls.delta_caller
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", caller):
            raise ValueError(f"{ENV_DELTA_CALLER} 只能是小写字母、数字与连字符（至多 32 个）")
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
            judge_key_file=Path(env[ENV_JUDGE_KEY_FILE]) if env.get(ENV_JUDGE_KEY_FILE, "").strip() else None,
            tool_loop=tool_loop,
            stream_deliver=stream == "1",
            task_service_internal_url=internal,
            delta_caller=caller,
            sub_agent_policy=sub_agents,
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
            sandbox_service_address=service,
            relational_dsn=env.get(ENV_RELATIONAL_DSN, "").strip(),
            sandbox_sessions=sessions == "1",
            sandbox_runtime=env.get(ENV_SANDBOX_RUNTIME, "").strip() or cls.sandbox_runtime,
            egress_gateway_url=env.get(ENV_EGRESS_GATEWAY_URL, "").strip(),
            preview_domain=env.get(ENV_PREVIEW_DOMAIN, "").strip(),
            workspace_allow_local_remotes=local_remotes == "1",
        )

    def store_paths(self, root: Path) -> tuple[Path, Path]:
        """ObjectStore 目录与密钥文件：配置了就用配置的，否则放在 root 下（与 task-service 的默认布局相同）。"""
        return self.object_store_dir or root / "objects", self.secrets_file or root / "secrets.json"
