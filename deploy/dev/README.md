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

不接真实端点时设 `AR_MODEL_PROVIDER=mock`（模拟模型，模型名为 `mock-small`、`mock-embed`，要配一份对应的 Profile 文件）。
