# deploy/dev

本地开发用的配置。

## model-profiles.json

Model Gateway 的开发用 Model Profile（设计书附录 H.1 的子集；格式与校验规则见
`services/go-model-gateway/internal/profiles`），由 `AR_MODEL_PROFILES_FILE` 指向。
模型取自开发用的 OpenAI 兼容端点（单供应商，聚合多家模型）；`vendor` 照模型所属的家登记，judge 据此用 `avoid_vendor` 选另一家。

| 档 | 模型 | vendor | Tier |
| --- | --- | --- | --- |
| small | gpt-4o-mini、gemini-3.5-flash-lite、claude-haiku-5-5 | openai、google、anthropic | `small@low` |
| mid | deepseek-v4-flash、gemini-3.5-flash | deepseek、google | `mid@low` |
| strong | claude-sonnet-5-5、gpt-5.5、claude-opus-5-5 | anthropic、openai | `strong@medium`、`strong@high` |
| embed | text-embedding-3-small | openai | `embed@default` |

选档取单价（输入加输出）最低的，其余是同档切换的顺序：small 首选 gemini-3.5-flash-lite，mid 首选 deepseek-v4-flash，
strong 首选 claude-sonnet-5-5。要固定用某个模型（例如评测时与临时 Gateway 对照用 claude-opus-5-5），在 CallContext 里设
`pinned_model_profile`。

**单价待按供应商实价更新。** 表里的单价（美元每百万 token）取自各家公开价格或最接近的同级模型，`freshness.price_at` 为 2026-10-21。
三个 Claude 模型用 Anthropic 一方 API 的标价（Haiku 5.5 的缓存读单价没查到，按输入单价记）；gemini-3.5-flash-lite、
deepseek-v4-flash、gemini-3.5-flash、gpt-5.5 是按同级模型估计的。端点（聚合网关）的实价可能都不同。
small 与 mid 档不发送推理强度（`thinking_budget` 为 0）；strong 档的 medium、high 分别用 8192、32768 的思考预算，
适配层按区间换成 `reasoning_effort`。各模型在端点上是否支持结构化输出与工具调用，按端点实测后修正 `capabilities`。

## 本地跑 Model Gateway

```powershell
$env:AR_SECRETS_FILE = "$PWD\data\secrets.json"           # 与 task-service、Worker 同一份
model-gateway dev-secret model-provider/api-key            # 从标准输入粘贴密钥；密钥不写进仓库，也不放在命令行参数里
$env:AR_MODEL_PROFILES_FILE = "$PWD\deploy\dev\model-profiles.json"
$env:AR_MODEL_PROVIDER_BASE_URL = "<OpenAI 兼容端点，形如 https://host/v1>"
model-gateway serve                                        # 默认监听 127.0.0.1:8091；计量记录写标准输出
```

开放注册或云上默认免费策略可使用 `default-tenant-policies.json`：它通过 `default.model_allowlist`
限制未单独配置策略的租户，只保留 small/mid 和 `gpt-6.1-sol` strong 档，同时保留两个 embedding Profile；
明确排除 `gpt-5.5` 与 `claude-opus-5-5`。租户显式条目优先于 default，未设置白名单的文件仍保持“不限制”的兼容行为。

本地运行时启用：

```powershell
$env:AR_DEV_TENANT_POLICIES_FILE = "$PWD\deploy\dev\default-tenant-policies.json"
```

云上使用 `values-cloud.yaml` 时会显式启用 Chart 内置的同等 ConfigMap；需要运维覆盖名单时，
把同一文件作为键 `tenant-policies.json` 创建外部 ConfigMap，再设置 Helm 的
`modelGateway.tenantPoliciesConfigMap`。默认 values 仍不挂载白名单文件。

不接真实端点时设 `AR_MODEL_PROVIDER=mock`（模拟模型，模型名为 `mock-small`、`mock-embed`，要配一份对应的 Profile 文件：
`mock-model-profiles.json`）。

## 本地拉起整套（T-13）

`scripts/dev_stack.py` 一条命令拉起 Temporal 开发服务、task-service（Registry 打开）、Model Gateway 与 Worker，细化见
`docs/plan/live-e2e.md`。真模型时供应商的地址与密钥只从 `~/.agentruntime/model-provider.env` 读，密钥经 `model-gateway dev-secret`
的标准输入写进这次运行的开发 SecretStore。

```powershell
uv run --no-sync python scripts/dev_stack.py up --tenant-effect t13=write_reversible   # 默认真决策、openai_compat
uv run --no-sync python scripts/dev_stack.py token --tenant t13 --user u13
uv run --no-sync python scripts/dev_stack.py kill worker; uv run --no-sync python scripts/dev_stack.py start worker
uv run --no-sync python scripts/dev_stack.py down
```

- `registry-seed.json`：启动前用 `task-service registry-seed` 种进 Registry 的开发种子：桩 Planner 与真 Planner 用到的 Node Template
  （`stub_answer`、`stub_confirm`、`answer`、`rewrite`），加上 `model-profiles.json` 里的全部 Model Profile。改了 `model-profiles.json`
  要同步改这里（`tests/e2e/test_dev_stack.py` 核对两边一致、每份文档合 schema）。租户策略不在种子里，由 `--tenant-effect` 种下。
- 运行目录默认是 `~/.agentruntime/dev-stack`（不在仓库里：开发 SecretStore 里有供应商的密钥）；平台内工具的工作区在其中的
  `workspaces/<租户>/<workspace>`。
