# File Intelligence — 服务器验收与主线合并记录

日期：2026-11-10。此记录接续 `file-intelligence-integration.md`，以这次实际服务器证据修正其先前未验证状态。验收在项目已有 SSH 主机 `cloudnium_cloud` 的独立目录 `/root/proj/agent-projs/.file-intelligence-validation-20261110` 运行，使用独立容器、测试数据库、Temporal namespace 和端口。未重启、迁移或替换生产服务。

## Linux 引擎与重算

完整处理镜像实际构建成功。规模解析和 UNO 的原始验收镜像为 `fi-20261110-engines:validation`（`sha256:fb638071d954329c16dba47b9292e4efec3dddd9075fe7fa35cfc2b18c3229d7`）；之后仅提升 OCR 警告可见性的最终镜像为 `fi-20261110-engines:final`（`sha256:b3a9cdcdda52b573259ebeaf00b9fb9a2b61a6d6232566c21ccb907c04f09f5e`）。旧/新引擎模块逐文件核对仅 `models.py` 响应层改变，UNO 模块及依赖不变。最终源码 SHA 记录见 `evidence/file-engines-server/engine-source-sha256.txt`。

实际环境为 Ubuntu 24.04、Python 3.12/匹配 pyuno、LibreOffice 24.2.7.2、Poppler、固定字体和 18 个离线模型文件。构建修复 musl 工具、CA、macOS-only 依赖 marker 和遗漏的模型仓库登记。模型/字体/系统包清单、首次失败与修复后的原始日志保存在 `evidence/file-engines-linux/`。

禁网、只读根文件系统、4 CPU/8 GiB 下，1900 与 1904 日期系统各 22 条公式从错误缓存 999 经 `calculateAll()`→`storeToURL()`→独立读取验证得到正确结果；另验本地 `Table1[Amount]` 结构引用。原件 SHA 不变，非目标隐藏 sheet、样式和图表结构保留。合法空字符串缓存的误判已修复。错误公式、循环引用、错误期望值、宏、外部链接均不能通过发布验证，超时后无该作业的 soffice 残留。XLOOKUP 在该 Calc 版本明确返回不支持的公式错误；不声称完整 Excel 兼容。

## 持久化、权限和集合

真实 Temporal CLI 1.9.1 / Server 1.32 完成七类验收：旧历史回放、API 幂等及固定输入、无 GET 也回写终态、杀 Worker 后真实心跳超时并恢复、单消费者取消保留共享任务、最后消费者取消拒绝迟到结果、两次失败/超时后有限结束。发现失败分支未持久化终态，已用统一 patch 增加有限的终态回写 activity；主处理最多 16 分钟、回写最多 3 分钟，在 20 分钟 workflow 预算内留余量。

子 Agent 的整张 worksheet 委派有真实端到端证据：Public sheet 可读并计算 42；Secret sheet、其他文件、sheet_id 别名、跨 sheet range、附加数据源、原始读取和 shell 均拒绝。旧 pending child 即使缺新增字段，也由可信 Temporal workflow_type 识别；并发父/子 ContextVar 不串。普通无文件任务的 coding 子 Agent 保留原有工具行为。此版仅承诺显式 whole-sheet 委派，不声称任意文件区域授权均已实现。

知识集合与原件附件分开授权。集合可包含最多 500 个已解析表示；Task 原件仍最多 10 个/200 MiB。用户可以显式选择 20 份资料的集合用于下一条任务：提交时冻结 generation，ObjectStore 仅写轻量集合目录，原始 attachments/versions 为空；集合访问不授予原件读取或原生图片权限。任务重试先核对原主体/会话/选择，再核对已冻结代次，不能从无集合扩权或改选集合；新代中无关来源撤权不阻断旧代合法重试。冻结集合的版本受到 GC 引用保护，逻辑删除仍立即阻断访问。

以上控制面在真实 HTTP、SQLite race、PostgreSQL 以及原件/派生图片授权边界验收通过，见 `evidence/file-collection-selection/`、`evidence/file-engines-m1/`、`evidence/file-engines-m3/`。

## 回归测试口径

- Task Service：630 个测试项（含子测试）/16 包通过；Model Gateway：191 个测试项/5 包通过；最新控制面再次测试及 vet 通过。
- Memory：211 项通过，包含真实 PostgreSQL/Qdrant 后端。
- Worker 完整运行：1649 通过、2 项旧 patch 约定检查失败、1 项付费 live Jev 用例未运行。修复后相关 patch/replay 子集 23/23 通过，Workspace 固定历史 3/3，本次保存的 19 份真实 File/Child 历史 19/19 回放通过。没有把这写成“重新全量 1651 通过”。
- 完整契约套件：638 项通过（仅排除会递归重复 Go 全仓矩阵的单项）；Buf lint/format/breaking 和 `uv lock --check` 通过。较早的 JSON Schema/跨语言/Provider 子集为 440 项。
- Coding 最终 Chromium/Go 全套：257 个顶层测试、335 个含子测试的通过事件，0 失败，vet 通过。140 个实际编译输入的本地/服务器 SHA 一致。设置保存期间发送、失败、编辑草稿和会话/账号往返的六类交错均通过。证据见 Coding 仓库 `docs/ui/evidence/`。

扩大的契约/部署联合测试最初为 1042 通过、4 失败。文件枚举未集中于共享 defs 的本次问题已修，uv 缓存权限通过隔离目录配置纠正；全部契约随后通过。另两项部署断言（既有 Cloudflare 标签格式、subagent=off 环境变量是否省略）已在未修改的 fdac343d 基线独立复现，并确认八份相关文件 SHA 相同；后续按实际已记录语义修正了两条旧断言：镜像标签额外允许既有 `goal-cf-YYYYMMDD` 日期发布族（仍拒绝 latest 和任意标签），`off` 必须明确传入值 `off`（仍检查默认 rules 和非法值拒绝）。部署配置未改；完整部署套件随后实际复跑 **408 通过、26 个 opt-in 镜像/kind 用例跳过、0 失败**。文件引擎镜像已在本次独立构建实测；跳过的全仓其他镜像/kind 测试不冒充完成。初始失败及基线复现保留。

初始失败、环境限制和修复后补验均保留，详细日志与命令见 `evidence/server-regression-20261110/` 和 `evidence/file-engines-server/`。仓库全部无关模块和付费评测不因本次相关测试通过而自动获得“全绿”声明。

## 规模评测与发布边界

冻结的非敏感合成集包含 120 个文件、690 个自动 golden 锚点和 100 条跨资料查询，输入 SHA 在运行前固定，未为抬分改动真值。真实镜像解析得到 688/690 自动锚点命中；两个 OCR 差异保留低置信标记，不当作正确原文。实测发现原先标记只在块详情中，最终镜像将它提升为顶层 `ocr_low_confidence` 警告、coverage 计数及 `partial` 状态；两问题样本重读的文本/ID/锚点/置信度/source 均与原结果完全一致，另有两份正常 XLSX 对照，未改评测真值。10 万行 CSV 精确聚合重复两次结果一致，5 种损坏/越界/不支持输入均明确拒绝。

检索评测使用真实 CPU multilingual E5、LlamaIndex/Runtime 组件和 Knowledge gRPC；模型是本地实际推理，不是哈希 embedding。向量后端为测试用 Runtime InMemoryVectorStore，账本为 SQLite，不能据此宣称已完成生产 Milvus 容量/延迟压测。初始 exact-BM25 与 hybrid 的 Recall@8 分别为 28.27% 和 29.60%，存在真实排序不足。保留这些结果，仅对可选 reranker 路径做一次固定方案对照：dense40+sparse40，同一 ACL，去重最多80，重排后取8。无 reranker 和 Memory 评分行为不变。

固定 100 条查询的最终重排对照完成：Recall@8 从原 hybrid 的 **29.60% 提高到 50.27%**，Hit@8 从 **79% 到 95%**，MRR 从 **0.559 到 0.930**；引用结构完整性均为 100%，不能把它等同于所有返回内容相关。重排 p95 **6.360 秒**，未达到设计中的 2 秒目标，因此仍为显式可选路径，不声称默认检索质量/性能全部达标。首轮长测的合成身份令牌过期已记录，修成每次 RPC 新签短期身份后，用同一语料/模型完整重跑 100/100，未关闭鉴权或改 gold。

最终检索对照指标、模型 SHA、延迟、两处 OCR 原因及逐题可审查资料以 `evidence/file-engines-benchmark/README.md`、`run/comparison.json` 为准。重排开关需要部署配置，不从“代码可运行”推导为质量达标。

**人工标注不被自动测试替代。** 当前人工复核锚点计数为 0；设计要求的 500 个手工核对锚点仍需独立人工审查。已提供 review HTML 与待填 CSV，不伪造 reviewer/date。代码主线合并表示实现、服务器工程验收和已列修复进入主线；不表示人工语义质量门、生产灰度或两秒检索 SLO 全部达成。

本次合并后不自动部署。生产启用仍需按镜像 digest 登记 file-engines runtime、配置服务凭据和检索模型、执行迁移/备份/有限 GC 调度，并按这里记录的能力矩阵验收；既有生产工作负载保持原状态。
