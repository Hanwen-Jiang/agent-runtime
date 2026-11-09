# File Intelligence 1.3 — 集成实现与验收出口

日期：2026-11-10。基于 `docs/design/file-intelligence-design.md` v1.3。

> 后续服务器实测、已修问题与最新合并验收状态见 [服务器验收记录](file-intelligence-server-acceptance.md)。下文保留首次本地集成交付时的状态；其中 Linux UNO、真实 Temporal 和规模自动评测的“未跑”已由后续证据更新，人工质量门另列。
本记录覆盖 M0.1 之后的实现，替代旧进度记录中“尚无解析/检索/计算/UI”的状态描述。
**代码已覆盖下面的主链；这不等于已上线，也不等于全部生产验收通过。**

## 已接通的范围

| 范围 | 实现与可见行为 | 证据 |
|---|---|---|
| 文件权威 | File 与不可变版本、原件/别名解析、当前 head、owner/显式 workspace 用户授权、版本 quota；任务固定原件和集合代 | Go SQLite race / PostgreSQL 手工探针 |
| 有界处理 | 既有 workspace 队列的新 FileOperationWorkflow；当前 ACL→源哈希→专用 Sandbox Service→真实 Python 引擎→派生对象→原子发布；共享消费者单独取消 | `evidence/file-engines-m1/bridge-acceptance.json` |
| 文档读取 | 文本、JSON/JSONL、CSV/TSV、PDF、DOCX、XLSX、PPTX、Notebook；ZIP/TAR 列举及显式成员提取；结构、覆盖范围、位置锚点 | `evidence/file-engines-m1/acceptance.json` |
| 视觉 | pypdf 快路径；复杂 PDF 使用 Docling standard pipeline + RapidOCR/ONNX；Poppler 页图，Office 私有 profile 渲染，原图区域裁切 | 同上，另 `chinese-ocr.json` |
| 原生看图 | 原图和授权渲染图使用 typed image refs；file_render→attach→下一模型请求；Gateway 再验 Task 根版本/current ACL/尺寸/MIME/SHA；文本模型不能默默接收图片 | `evidence/file-engines-m1/tools-acceptance.json` |
| 检索 | `llama-index-core` 内部组件复用现有 Gateway/VectorStore；Memory 与 document_chunks 共用引擎、不同 corpus；集合 CAS 发布、任务固定 generation、引用二次鉴权 | `evidence/file-engines-m2/` + Memory 194 项（模型返回使用可控替身，非商业模型检索质量评测） |
| 表格 | DuckDB 参数化只读查询、显式多源 join、先物化选区、拒绝外部文件/网络/写 SQL；XLSX 区分公式与保存缓存 | `evidence/file-engines-m3/engine-results.json` / `join-results.json` |
| 编辑 | DOCX run/PPTX shape/XLSX cell 的指定 XML member 修改；原件保留、候选独立；L1 结构校验，L2 独立重建及非目标区域渲染比对；冻结 base/candidate/criteria 后才能 CAS 发布 | `evidence/file-engines-m3/visual-results.json`、authority 日志 |
| Notebook | 读取不执行；显式执行仅允许受控禁网 Sandbox；执行指定 cells，候选保留未选 cells 的 metadata/output；本机回退禁止执行 | `evidence/file-engines-m3/notebook-selection-results.json` |
| Excel 重算 | Linux 镜像中的匹配 Python UNO 调用 `calculateAll()`→`storeToURL()`→公式、错误与预期数值校验；结果不能以 openpyxl 或格式转换冒充 | **代码/依赖已实现；真实 Linux UNO 执行验收待补** |
| 生命周期 | 删除先撤权；30 天回收站；任务/集合引用保护；24 小时孤儿、30 天未用派生缓存、90 天审计清理；持久 GC 队列与有界 CLI，可重试 | `evidence/file-engines-m3/authority-*.log` / `gc-cli-result.json` |
| React 工作台 | HeroUI Pro；预览/结构/SQL/编辑/版本/权限/回收站；多文件索引；真实进度轮询、取消、错误/覆盖提示、显式候选发布；桌面/手机布局及 reduced-motion | Coding 仓库 `docs/evidence/file-workbench/`（截图明确为交互 fixture） |

## API 与工具出口

- 原有 `/v1/uploads`、`/v1/files`、`/v1/files/adopt` 保持兼容；认证的 `source_uri` 是不带凭证的不可变对象引用。
- `GET /v1/files/{id}/details` 返回版本、处理记录、变更集与 `permissions`。
- `POST /v1/files/{id}/actions`：rename、grant、revoke、trash、restore；`GET /v1/files/trash`。
- `GET /v1/files/{id}/versions/{version}/content` 始终读取指定版本。
- `POST /v1/file-jobs` 接受 operation、request_id、1–10 个 file/version 输入、selector/options；不接受浏览器文件路径作为权限。
- `GET /v1/file-jobs/{id}`、`POST .../cancel`、`GET .../artifacts/{artifact}`。
- `POST .../attach` 将渲染图授权给根版本都已固定的 Task；`POST .../complete` 仅限可信 Worker 回写，关闭网页也能持久化终态。
- `POST /v1/files/{id}/publish` 必须给 changeset_id、expected_head、verification_digest；当前权限/head/epoch 任一变化拒绝发布。
- `GET /v1/knowledge/collection`、`POST /v1/knowledge/index`、`POST /v1/knowledge/search`；集合与 generation 由服务端决定。索引只覆盖显式读取的表示，未读取部分不假定已覆盖。同一 PDF 不同页段的 representations 可同时索引。
- Worker 工具：`file_inspect`、`file_read`、`file_render`、`table_query`、`file_edit`、`file_search`、`evidence_resolve`。来源必须在当前 Task 固定清单内；工具不自动发布候选。
- Coding 代理仅转发当前已鉴权会话的 workspace，客户端不能指定上游或租户。文件工作台通过右侧来源入口/文件项打开。

## 部署约束与开关

1. 同步迁移 410–414 和检索 421；服务共享 tenant-bound Relational/ObjectStore。先部署控制面迁移，再开启文件引擎。
2. 构建 `deploy/images/file-engines/Dockerfile`，生成并记录不可变镜像 digest。18 个模型文件的 revision/SHA 已锁定，系统包使用固定 Ubuntu snapshot，输出字体与系统包清单。运行时不下载模型。
3. 在已有 Sandbox backend 的 `images` 映射中登记 `file-engines` 为该 digest，禁网、无项目工作区挂载、无项目 setup/env 注入。既有 workspace Worker 池须运行。
4. Helm `fileIntelligence.enabled=true`；配置独立 `internalTokenSecret`、`knowledgeTokenSecret` 与 `memory.embeddingVersion`。内部 Worker 文件请求使用专用服务凭据，公开入口仍使用原身份认证；Knowledge 由控制面根据已验证身份签发短期断言。
5. 原生图像能力还要求真实模型 profile 开启 image_input，所有回退候选同样支持；部署能力不足会显式拒绝，不声称已看图。
6. `task-service file-gc --tenant T --workspace W --limit 100 --prefix uploads/` 与 `--prefix file-derived/` 是有限维护入口；按 next_cursor 分页。部署需要将其接入现有维护调度。没有新增常驻队列、daemon 或另一套 Agent 工作流。
7. 备份保留 30 天由部署的数据库/ObjectStore 备份配置落实；应用 GC 不声称能删除外部备份。

容量：50 MiB/文件，10 文件和 200 MiB/Task，500 文件及 2 GiB/会话（保留版本计费）；派生累计 512 MiB/job、单产物仍最多 50 MiB。原生视觉另限 10 MiB/张、20 MiB/次、10 张、20MP/8192 边长。压缩包引擎的实际保守上限为 200 MiB/1000 成员，不等于设计的最大可选档。

## 本次验收与明确未通过项

已实际运行：M1 21 项解析/渲染/OCR、桥接 16 项、工具链 8 项；M3 SQL/编辑/视觉/Notebook 与 SQLite-race/PostgreSQL 权限并发探针；Memory 194 项、契约 440 项；Coding Go+Chrome 两包通过；Helm lint 和启用后的 manifest 渲染通过，相关镜像/云配置 93 项通过。

扩大回归时，Worker 使用可运行的 Go sandbox-agent 对照后 **1254 passed、2 failed、1 error、1 skipped、394 deselected**。剩余失败涉及旧 Go agent 不具备的 transfer-live-chain 行为和缺 Temporal CLI；默认 Rust agent 需要本机没有的 rustc 1.99。完整 deploy suite 另有未安装 learning/collector 工具和既有 Cloudflare tag/subagent-off 断言差异，不能写成全仓绿灯。本次修改的相关检查与有限真实链路另行通过，不篡改不相关功能来掩盖这些结果。

**发布出口仍有三项环境/规模验收：**

- Linux file-engines 镜像实际 build/run，匹配 pyuno 的 Calc 重算小样本与回归。当前宿主 Docker 不可用，宿主 LibreOffice 渲染成功不能替代这一项。
- 新 FileOperationWorkflow 在真实 Temporal 服务中的恢复/取消验收。代码使用原队列，真实 Sandbox Service 桥已跑；缺 CLI 的工作流服务检查未跑。
- 设计中的 120 文件/500 锚点/100 检索问题完整评测及生产灰度。当前小样本证据不等于该规模指标。

本次代码提交以以上范围为出口，不扩展音视频、远程连接器、在线 Office 多人协作、自研完整 Excel 引擎或另一套 Agent/队列。整体生产完成状态必须等上述验收补齐后再标记。
