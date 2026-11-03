# 模型目录自动同步（F1，N-07 的延续）

> 状态：2026-11-03 设计稿与验收测试（`tests/deploy/test_model_catalog_sync.py`），实现等人看过测试再写。登记在 `work-dag-2026-11-03.md` 的 F 线。

## 1. 要解决的

人的原话：可用列表太少，好多又是过期模型；系统应该经常拉取中转站的 `/models`，每家都选几个最新的代表性模型。

**过期**的定义（人定）：同一条产品线出了新版本，旧版本就算过期，比如有了 gpt-6.1-sol 还在用 gpt-6-sol。

现在的做法是人手工改 `model-profiles.json` 与白名单，所以线上 11 个 Profile 里有 gpt-5.5、gpt-4o-mini 这类早已有新版本的，main 上新加的 11 个也没进任何白名单。

## 2. 做法

新脚本 `scripts/sync_model_catalog.py`（只用标准库，复用 `sync_model_profiles.py` 的取价与写文件），每次运行都从头算一遍，不记状态：

1. **输入**：
   - 中转站的 `GET /models`：地址与密钥取 `AR_MODEL_PROVIDER_BASE_URL`、`AR_MODEL_PROVIDER_API_KEY`，或 `model-provider.env`；密钥不进输出、报告与错误信息；
   - models.dev 的 `api.json`；
   - 规则文件 `deploy/dev/model-catalog-rules.json`；
   - 现有的 Profile 文件与租户策略文件。
2. **产品线**：从模型名去掉版本号得到线的键，版本号用于比较新旧。
   - 例子：`gpt-6.1-sol` → `gpt-*-sol`、6.1；`claude-opus-5-5` → `claude-opus-*`、5.5；`qwen3.8-max` → `qwen*-max`、3.8；`kimi-k3.1-pro` → `kimi-k*-pro`、3.1。
   - 线的全名带上 vendor（`openai:gpt-*-sol`）。
   - 规则文件的 `aliases` 处理改名换线：比如 `openai:gpt-*`（gpt-5.5）接到 `openai:gpt-*-sol`，`openai:gpt-*o-mini` 接到 `openai:gpt-*-luna`。
3. **哪些算候选**，要同时满足：
   - 中转站上有；
   - models.dev 里有**一方**条目（vendor 自己的供应商，2026-11-02 人定的取价规则）且只输出文字；
   - 不是快照或变体：结尾是 4 位、8 位日期，`-expires-on-…`、`-preview`，`ignore` 里的模式（vision、image、audio、realtime、omni、`-exp` 等）。

   没有一方价格的（例如中转站上的 deepseek-v4.1-flash、minimax-m3）不自动加。如果它是某条在跟的线上更新的版本，报告里列出来，附第三方价格的中位数作参考；人在规则文件的 `price_overrides` 里给了价格才加。
4. **每条线的当前版本**：按版本号比新旧，版本号相同再看发布日期。两者先后不一致时按版本号，并写进报告（例子：models.dev 里一方的 deepseek-v4-flash 标的发布日比 v4.1-flash 晚）。
5. **Profile**：
   - **一条线只有当前版本可用**，旧版本写 `availability: false`（schema 已有这个字段，网关选档已经排除它）。Profile 不删，固定了旧版本的 Task 和历史仍然查得到。
   - 中转站上没有了的也写 `availability: false`，再出现时自动恢复。
   - 人要单独停用的写进 `disabled`。
   - 可用的 Profile 不写这个字段，和现在一样。
6. **新版本**：在跟的线（已有 Profile 的线，加上规则文件 `track` 里的线）出了新版本，就加 Profile。
   - 档位（class）与 Tier 继承同一条线上的旧版本，`track` 里的新线按规则给的。
   - 能力（structured、tools）、单价、长度、发布日来自 models.dev。
   - **不继承** `capability_scores` 与 `reliability`：这两个是对具体模型的测量，新模型用 0.5 的先验，等能力矩阵测了再写。
   - 新 Profile 按模型名排序追加在文件末尾，id 照旧是 `a6api/<模型>@<年-月>`。
7. **新线**：不在跟的线，只列进报告，附按名字与单价猜的档位；人把它加进 `track` 才会加 Profile。
8. **白名单**：按规则重算租户策略文件里各个 `model_allowlist`。仍然是显式的列表，不改契约。
   - `default`（免费档）：在服务的线里，可用的 small、mid、embed，加上单价不超过上限（默认输入 2、输出 10 美元每百万 token）的 strong。
   - 内部租户：所有在服务的、可用的 Profile。
   - `swebench`：同上，再去掉 anthropic（中转站给 Claude 注入工具，见 TMP 计划）。
   - `evals`：另外包括候选线（`track` 里标了 `candidate` 的，例如 mimo-v2.6-pro、LongCat-2.0），供能力矩阵测。

   其余字段（`model_budget`、`ceiling`）原样保留。规则里有、文件里没有的租户不新建，只在报告里提一句。仓库里的 `default-tenant-policies.json` 与 values.yaml 的 `modelAllowlist` 一起改，保持一致。
9. **单价与长度**：照 `sync_model_profiles.py` 的规则同步（两个脚本共用代码）。
10. **防误删**：拉不到 `/models`，或者现有可用 Profile 里有一半以上不在列表上（多半是中转站临时出错），就不改任何文件，退出码非 0。
11. **报告**：
    - 内容：加了哪些、停用了哪些（原因与接替者）、恢复了哪些、没有价格的新版本、新线、版本号与发布日不一致的、各白名单的增减；
    - 格式：Markdown 与 JSON 各一份（`--report <目录>`）。
12. **`--check`**：只比较，有出入时退出码 1，不改文件。

## 3. 怎么定期跑（要人定）

| 方案 | 做法 | 好处 | 代价 |
| --- | --- | --- | --- |
| **A（建议先做）** | GitHub Actions 每天跑一次（`model-catalog.yml`），有变化就开或更新一个 PR，报告当 PR 正文；人合进 main，随下一次部署上线 | 改动可审，和现在的部署流程一致；网关不用改 | 要把中转站的地址与密钥放进仓库的 Actions Secrets；上线要等部署 |
| B | 集群里的 CronJob 直接改 ConfigMap，经 Registry 发布 Profile，再让网关重新加载 | 真正自动 | 网关现在只在启动时读 Profile，要加重新加载；自动改生产，出错面大 |

建议先 A，跑顺了再考虑 B。

## 4. 风险

- **选档会偏向新模型**：网关排序里发布日占 35%，没测过的能力按 0.5 算。某条线进了免费档的 strong，最新的那个就会成为首选。按现在的规则，免费档的 strong 首选会变成 kimi-k3.1-pro（2026-10-23 发布）。要避免，就把那条线留在候选里，先测再放。
- **中转站对 Claude 不是直通**（会注入工具、结构化输出不稳）：免费档里现在已经有 claude-haiku-5-5，这次不改变这一点；swebench 照旧去掉 anthropic。
- 线的键是按名字算的。厂商改名（gpt-5.5 → gpt-6-sol）只能靠 `aliases`，报告里会提示“同一家有更新一代、但不在同一条线”的情况。

## 5. 要人定的

| # | 问题 | 建议 |
| --- | --- | --- |
| 1 | 定期跑用 A 还是 B | A；同意的话，要把中转站地址与密钥放进 Actions Secrets（人放，不经聊天） |
| 2 | 免费档 strong 的单价上限 | 输入 2、输出 10 美元每百万 token：sonnet-5-5、gpt-6.1-sol、gemini-4-argon、kimi-k3.1-pro、qwen3.8-max、glm-5.3、grok-4.7、deepseek-v4-pro 在内，astra、fable、opus 不在。每天 3 美元的预算照样兜底 |
| 3 | deepseek-v4.1-flash 的价格 | models.dev 的 DeepSeek 一方条目里没有它（一方的 deepseek-v4-flash 标的发布日 2026-10-17，像是指向 v4.1 的新快照）。建议先不加，等 models.dev 补上；急用就在 `price_overrides` 里按一方 v4-flash 的价（0.15/0.6）给 |
| 4 | 哪些线放进服务、哪些只当候选 | 服务：人点名的各家最新线（OpenAI 的 astra、sol、terra、luna；Claude 的 fable、opus、sonnet、haiku；Gemini 的 argon、flash、flash-lite；DeepSeek 的 flash、pro；GLM、GLM flash；Qwen max、flash；Kimi k3.1-pro；Grok）。候选（只给 evals）：mimo-v2.6-pro、LongCat-2.0；minimax-m3 没有一方价格，先不加 |

## 6. 验收（对应测试）

1. 线与版本按第 2 节第 2 条解析；快照、变体、预览、非文字输出的模型不算候选。
2. 同一条线出了新版本：加新 Profile，档位与 Tier 继承旧版本，不继承测量；旧版本 `availability: false`，报告写明接替者。
3. `aliases` 接起来的线照样停用旧版本（gpt-5.5、gpt-4o-mini）。
4. 没有一方价格的新版本不加，报告附第三方价格的中位数；旧版本保持可用。给了 `price_overrides` 就加。
5. 版本号与发布日先后不一致时按版本号，写进报告。
6. 中转站上没有了的停用，再出现时恢复；`disabled` 里的总是停用。
7. 新线只进报告；`track` 里的新线按规则加。
8. 白名单按第 2 节第 8 条重算：只有可用的；免费档的单价上限；`exclude_vendors`；候选只给标了的租户；其余字段原样；不新建租户；顺序跟 Profile 文件一致。
9. 种子、默认策略文件与 values.yaml 的 `modelAllowlist` 跟着改，保持一致；values.yaml 其余部分一个字节不变。
10. 同样的输入跑第二遍没有改动；`--check` 有出入时退出码 1、不改文件。
11. 拉不到列表、或一半以上的可用 Profile 不在列表上：不改文件，退出码非 0。
12. 密钥不出现在标准输出、标准错误与报告里（对着一个要求认证的本地假中转站测）。
13. 仓库里的规则文件引用的线、租户与 Profile 都对得上。
