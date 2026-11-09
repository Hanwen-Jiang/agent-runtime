# 文件智能 M0.1：Runtime 文件目录与兼容入口

状态：M0.1 集成本地验收已完成（integrated local verification done）；以设计基线 `92b84ff6` 为起点，Runtime Catalog、HTTP 入口与容量标准档已接线，尚未线上发布。
日期：2026-11-10。  
工作项：文件智能设计 M0 的首个可交付切片；HTTP JSON 契约按 S-01 流程审阅。

## 本轮结果与边界

本切片计划让 Runtime 持久保存逻辑文件与不可变版本的元数据，按当前用户和 workspace 授权读取。新上传登记到该目录；旧上传在其 ObjectStore 元数据得到验证后可懒导入。提供分页文件列表、单文件及其 head 版本的元数据读取，同时保持现有 Task attachments 和下载入口兼容。本轮不单独新增版本 GET 接口。

本轮不实现 Representation、SourceAnchor、处理 Job、知识索引、共享 ACL、删除/回收、内容编辑或新版本写入 API。文件进入目录仅表示原件已登记，不表示已经解析或理解。M0.1 完成不代表主设计的整个 M0 已完成。

## 已冻结的模型

| 模型 | HTTP JSON 字段 | 约束 |
| --- | --- | --- |
| RuntimeFile | `id, workspace_id, name, created_by, created_at, head_version_id, lifecycle` | `id` 复用规范 `src_<32hex>`；历史 source ID/URI 通过 alias 保留；`created_by` 是本轮唯一 owner；`lifecycle` 只声明 `active` |
| FileVersion | `id, file_id, mime_type, size_bytes, sha256, created_at, created_by, parent_version_id?` | `ver_<32hex>`；原件 SHA 与大小来自经过验证的 ObjectStore 记录；无父版本时省略 parent 字段 |
| Record | `file, version` | 不向浏览器暴露 object URI；版本所属 file 必须一致；head 读取还核对 head_version_id |
| List response | `files: Record[], next_cursor` | 先按身份与 workspace 授权，再分页；游标必有，末页为空串；游标不改变权限 |
| Adopt request | `object_uris: string[]` | 1–500 个旧引用；不接受客户端 owner/name/SHA 等权威元数据；原子成功或全批失败 |
| Capabilities | `max_file_bytes, max_task_files, max_task_bytes, max_workspace_files, max_workspace_bytes` | 反映当前文件容量；与模型原生图片的输入限制分开 |

共享结构见 [file_catalog.schema.json](../../schemas/file_catalog.schema.json)。`get_response` 与 `record` 是同一形状，只定义一次；根 `oneOf` 区分 record、list、capabilities 与 adopt request。Schema 检查字段、类型和格式；鉴权、引用关联、同一上传并发登记及游标绑定由服务端检查。

租户继续由 `Relational.Bind(tenant)` 隔离，不给业务 SQL 添加 tenant 列。ObjectStore 保存原始字节，版本行内部保存 object URI。Runtime 标准档为 50 MiB/文件、10 文件/Task、200 MiB/Task、500 文件/2 GiB workspace（会话），允许空文件。能力接口返回字节值分别为 `52428800`、`10`、`209715200`、`500`、`2147483648`。MIME 与上传容量不等于解析能力；原生图片模型的每图字节、像素和总量限制不由这里放宽。

## HTTP 与兼容接线

本轮路由如下，均使用现有 Bearer 身份和服务端 tenant 绑定：

| 路由 | 输入与响应 | 约束 |
| --- | --- | --- |
| `GET /v1/files/capabilities?workspace_id=...` | 返回五项容量值 | workspace 必填；未配置 ObjectStore 时明确失败 |
| `GET /v1/files?workspace_id=...&limit=50&cursor=...` | Page，默认 50 项，limit 1–100 | 最新创建时间优先，再按 ID 降序；keyset cursor 绑定 tenant/owner/workspace |
| `GET /v1/files/{file_id}?workspace_id=...` | Record，包含 head 版本 | 支持历史 source alias；跨主体/作用域按不存在处理 |
| `POST /v1/files/adopt?workspace_id=...` | `{object_uris:[...]}` → Page | 1–500 项；按输入顺序返回规范 Record；`next_cursor` 总为空；先验证所有对象再事务登记 |
| `POST /v1/uploads?workspace_id=...&filename=...` | 原始文件字节 → 既有上传响应，加 `file_id/version_id` | 保持旧 `id/uri/name/mime_type/size_bytes/sha256/created_at`；先登记目录再返回成功；URI仅用于现有内部代理链，不出现在 File API 公共Record |
| `GET /v1/uploads/{upload_id}/{filename}?workspace_id=...` | 既有受鉴权下载 | 保留旧源 ID 与原件引用；校验后懒导入，不覆盖原件 |

Task Submit 保留原 `attachments: obj://...[]`，在创建 Task/调用 DurableEngine 之前核对 10 文件和 200 MiB 累计大小。应用层 `source_ids` 的投影由 Coding Agent 兼容代理负责，不新增 protobuf 服务。

实现入口：[File HTTP](../../services/go-task-service/internal/taskapi/files.go)、[上传兼容](../../services/go-task-service/internal/taskapi/uploads.go)、[Catalog](../../services/go-task-service/internal/files/catalog.go)。

1. 上传注册沿用现有上传入口与服务端 MIME/SHA 生成规则；完成登记后再报告目录可见。
2. 旧上传懒导入验证对象实际存在、owner、workspace、文件标识、MIME、大小和 SHA；客户端提供的元数据不作为权威。
3. 文件列表只返回当前 owner/workspace 可见记录；分页的排序、上限和无效游标行为在 handler 中固定。
4. 文件与版本读取在每次请求重新鉴权；不通过知道 ID 或 URI 扩大访问。
5. 旧 Task attachment 引用、任务 manifest 和文件下载语义保持兼容；不会自动为旧文件执行解析、索引或修改。

## 持久化与迁移要求

Runtime catalog 是文件身份与版本元数据的权威。应用层列表/JSON 是可重建的投影或迁移输入，不能并行决定 owner、workspace 或版本关联。上传对象先于目录登记时可能留下不可见孤立对象；必须可重试登记，不应把失败伪装成成功或删除仍被任务引用的对象。

同一旧上传并发懒导入应得到同一逻辑文件和版本；使用数据库唯一约束与事务处理，不依赖进程内 map 去重。相同 owner/workspace/name/hash/size 的旧上传归并为一个规范 File/Version，每个旧 source ID/URI 都登记 alias，历史下载与任务引用不丢失。`RegisterBatch` 在同一事务中处理全批；新批次按 source ID 排序决定规范身份，但返回保持输入顺序。已登记记录与对象元数据冲突时明确失败，不能悄悄覆盖原件或 owner。全域迁移 `410` 创建目录、版本、alias 与 workspace 锁表，生产启动和既有 Task API fixture 均注册该迁移。

上传、adopt 与下载读取使用实例级两个全文件槽位限制并发；当前 ObjectStore 接口接收 `[]byte`，因此这是逻辑负载限制，不等同于进程 RSS 上界。流式 ObjectStore 与更大文件容量不在本轮内。

## 验收与本轮出口

不新建永久测试文件；复用现有 schema 样例和契约检查，并以可复现的现有服务/临时探针覆盖实际行为。

| 场景 | 通过证据 |
| --- | --- |
| 新上传登记、重启后读取 | 文件与 head 版本 ID、SHA、大小保持一致 |
| 旧对象懒导入与重复/并发调用 | 校验 owner/workspace 后登记，重复结果不生成第二个版本 |
| 跨用户、跨 workspace 和跨租户读取 | 列表无泄漏；直接读取拒绝；无效/外部 URI 不能导入 |
| 文件列表分页 | 稳定顺序、无重复遗漏、末页游标和参数校验明确 |
| GET 文件及其 head 版本 | 返回 Record 公共投影；不含 object URI/原始字节；关联不一致时拒绝 |
| 旧附件兼容 | Task attachments、manifest 和下载路径的既有检查仍通过 |
| JSON 契约 | 正例通过、反例按预期路径失败；M0 文件 API 不改 proto；同分支补齐的上一轮原生图片协议/生成代码单独通过 Buf 兼容检查 |

退出条件：以上范围有通过证据，阻断缺陷为零，确切 HTTP 路径/分页参数和检查结果已补齐；剩余 M0 能力记录为后续切片。未运行线上发布时明确保留为未验证项，不用本地通过替代线上结果。

## 验收记录

- 既有 `test_schema_examples.py` 与 `test_enum_parity.py` 共 418 项通过，包含本轮 7 个正例和 13 个反例，没有新增永久测试文件。执行时 `AR_REPO_ROOT` 指向本 worktree，避免使用共享 checkout 的契约。
- `go test ./internal/taskapi ./internal/files ./cmd/task-service` 通过；files 包无永久测试，行为使用临时探针验收。
- 真 HTTP + SQLite + diskobjects + localingress 探针通过：容量协商、上传入目录、4 次实际 50 MiB 文件成功、50 MiB+1 返回 413 且不登记、历史 alias/get/download、跨 user/workspace/tenant、分页及 DB 重启恢复、valid+foreign adopt 全批拒绝，以及 200 MiB+1 Task 在 DurableEngine 调用前拒绝。
- 最终捕获的 15 个真实 capabilities/record/page/adopt HTTP 响应均通过 `file_catalog` schema 校验。证据位于临时目录 `m0-http-evidence-2601602357/public-responses.json`，同目录保存 `http_probe.go` 与复跑说明；不作为仓库运行依赖，临时 Go package 已移出仓库。
- 探针发现并关闭完整性顺序问题：旧下载现在先验证字节 SHA/大小/MIME，再登记目录；Task 先验证全部来源，再 `RegisterBatch`。最终复验中损坏下载返回 503、损坏 Task 提交被拒，两者目录均保持零记录，DurableEngine 未被调用。
- 正向兼容复验通过：旧 alias URI 可成功提交 Task，恰好一次调用 DurableEngine，任务 manifest 保留输入的旧 URI，不被目录去重改写。
- Coding Agent 既有来源浏览器测试新增标准容量场景通过（390px、真实 Chrome）：session/project 能力协商后 11 MiB 文件经过实际 multipart 上传成功；50 MiB+1 在浏览器拒绝且不上传；新草稿选择 5 个 50 MiB 文件仅保留前 4 个，200 MiB 总量后拒绝第 5 个且未触发创建会话/上传/Task；历史 Task 的旧 `source_id` 经 `alias_ids` 恢复附件链接。旧服务器 10 MiB fallback 场景保留。
- 未运行线上发布；本地检查不能替代部署后验收。Coding Agent 迁移和前端容量展示由对应集成检查另行记录。
- M0 文件 HTTP API 本身没有修改 protobuf；同分支补齐上一轮原生图片输入的 proto/Go/Python 生成代码依赖。此 worktree 的 `buf lint`、`buf format --diff --exit-code`、`buf breaking --against '.git#ref=HEAD'` 与原基线检查全部通过，不更新 `proto/breaking-baseline.binpb`。

## 当前检查点

主设计的 Python 文件引擎与统一 LlamaIndex 检索选择属于后续 M1/M2，本轮未安装解析/OCR/检索依赖。M0.1 集成本地验收通过，已发现的完整性顺序问题已关闭；没有把本地验收等同线上发布，也不宣称整个 M0 或 M1/M2 已交付。
