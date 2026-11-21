# Mini 50 的 Runtime 两臂与实际模型审计

隔离设置于 2026-11-21 在 cloudnium 准备。这个目录的预检只证明服务、工作区与模型固定路径运行正常，不计入两道 Sphinx 或正式 50 题分母。

| 项目 | A | B |
|---|---|---|
| 源码基线 | `da58416909fc54f02519de95706df01d9bbd1701` | 同一基线，仅叠加本次 Reviewer 的两处文件 |
| Git 树 | `c095dc8a858f4acd798396dc898f1ea3c176e906` | `014d63df7e525c09f03e52fc226f3ea88bacf198` |
| Temporal namespace | `ar-mini50-20261121-a` | `ar-mini50-20261121-b` |
| Task API（集群内部） | `mini50-1121-a-api:8080` | `mini50-1121-b-api:8080` |
| 生成模型及推理档 | 两臂均为 `a6api/gpt-6.2-sol@2026-11`，`strong@medium`（该档配置的 thinking budget 8192） | 同 A |

源树通过临时 Git index 构造，原工作树中的评测网站、RAG、新数据集等更改没有进入 B。两个环境复用相同部署镜像和 sandbox 服务，任务、工作区、决策、程序、模型五类 Worker 只从各自 Temporal namespace 取工作。生成调用使用专属网关的 RequiredProfile，覆盖 worker 各生成角色和摘要/探针；冲突的调用方 pin 会被拒绝而不是回退。两臂均关闭额外外部 Jev 和共享远程 Memory（避免跨臂记忆以及复制开发签名权）。Registry、对象存储及数据库使用已有服务工作负载身份。新 Task API 仅信任限制到 `swebench`/`evals` 的 ops *公钥*，拒绝开发令牌。评测操作不改线上原部署、原 Service、租户白名单或推理策略。

## 真实预检

- [源码与逐文件 SHA](freeze.json)；[隔离 Deployment、控制器与镜像 ID](deployment-observations.json)。
- [工作区检查](workspace-preflight.json)：隔离 API 各建一份带测试文件的 git 工作区，检查 HEAD，并显式 Archive 取得快照后删除。普通 gVisor 创建不默认返回快照。
- [模型正向探针](pin-preflight.json)：该文件保留早期 GPT‑6.1 预检历史，不能作为当前 6.2 正式证据。当前正式运行必须重新取得两个 Worker 经专属网关的 GPT‑6.2‑sol 响应，且每道题从 `model_usage` 与隔离 meter 审计实际 Profile。错误显式 pin 的独立预检在 provider 调用前返回 `routing_override_pin_conflict`。
- [两臂独立账本比对](pin-registry.json) 为同一平台 `v2` Registry 摘要，正式每次调用均要匹配该摘要。证明的版本粒度是网关返回的 model profile、registry 版本/摘要与计量流水；第三方通道未提供独立的上游权重指纹。单次预检不能代替每道正式题的完整审计。

## 复现与导出

`freeze.py OUT` 仅将基线与本地两个候选源码文件冻入 OUT。复制 `freeze.json`、`candidate/` 与本目录脚本到评测节点的同一私有目录。现用目录为 `/opt/evals/swe-mini50-20261121-runtime`。预检另需把项目的 `services/py-learning/src/agentruntime_learning/benchmarks/client.py` 放在该目录并命名为 `runtime_client.py`。

```sh
# 只操作带本实验所有权的对象。先验证不与任何现有 Service 选择器重叠。
python3 cluster_arms.py check
python3 cluster_arms.py apply
python3 cluster_arms.py status
# 在正式模型运行前启动，持续收集专属网关计量行；不采集 prompt、回答或密钥。
python3 meter_watch.py meter.jsonl
# 每一臂正式收集结束后（可重复执行），在该运行目录生成 model-audit.json。
python3 audit.py /opt/swebench/runs/RUN_A a --tenant swebench
python3 audit.py /opt/swebench/runs/RUN_B b --tenant swebench
```

审计按 instance_id 找到终态记录的 task_id，读取该租户 RLS 的只读 model_usage，逐任务按节点、方法、Profile 与费用与隔离网关 meter 核对，tier 从实际 meter 取；再核对应 namespace 的 TaskWorkflow 数。丢失流水、未结算费用、缺少 registry 摘要、错误 Profile 或非唯一采样不能当作有效对照。沙箱镜像另查每题 `environment_used`；没有实际镜像观察时不从 manifest 宣称已用官方环境。清理前先保留运行目录和审计文件；`cluster_arms.py delete` 遇到运行中的 workflow 会拒绝删除。
