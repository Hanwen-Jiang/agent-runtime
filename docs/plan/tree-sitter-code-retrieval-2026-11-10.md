# Tree-sitter 代码解析与检索接入

日期：2026-11-10。作为 File Intelligence 的源码读取和统一检索扩展，基于 `codex/file-intelligence-m0` 实现；不修改 proto、schemas、数据库迁移或工作流命令序列。

## 实际链路

`file_read` / 文件读取 API → FileOperationWorkflow → 原有 Sandbox Service 文件引擎 → Tree-sitter 代码表示 → 原有 Knowledge 索引 → 检索与引用解析。

原文件服务负责不可变版本、租户与文件权限；桥接下载时校验哈希、保留可信文件名后缀。新代码只处理交给引擎的源码，不自行扫描工作区，不下载语法包，不执行文件或导入其中的模块。

## 解析与读取

- 支持 Python、JavaScript/JSX、TypeScript/TSX、C、Go、Rust。前三类与 C 使用原文件镜像锁定的语法包；补充锁定 `tree-sitter-go==0.25.0` 与 `tree-sitter-rust==0.24.2`。处理器指纹包含各包版本。
- 提取函数、类、方法及支持的类型声明。`selector.symbol` 按名称或限定名读取，如 `Cart.total`、`Client.Run`；`start_line` / `max_lines` 保留严格行选区语义。
- 每个代码块带 `kind=code`、`anchor.kind=code_range`、语言、符号及限定名、绝对 UTF-8 字节范围和从 1 开始的行号。字节末端不包含在范围内，尾部换行不计为下一空行。
- 默认每块最多 2400 UTF-8 字节。优先使用定义和语法子结构边界，超长行按 UTF-8 边界切分，并标明符号仅部分覆盖。导入、注释、顶层语句和空白保留；总输出仍限制为 500 块、40000 字符，裁剪同时更新引用范围。
- 语法输入最多 4 MiB，遍历最多 100000 节点/5000 符号，符号名或限定名最多 512 UTF-8 字节。错误语法、缺少语法包、不支持的语言、指定非 UTF-8 编码或超出上限时显式回退文本读取；无法解析时不伪造符号查找结果。
- 解析范围按内容哈希、语言、parser/grammar 版本缓存，仅保留不可变元数据，不共享 native Tree 或授权信息。缓存最多 32 项、约 8 MiB 元数据，语法变更重解析该文件。当前 CLI 每请求一个进程，因此该内存缓存不会跨 CLI 进程命中；不宣称已实现编辑增量树更新或全仓增量索引。

工具示例（URI 必须属于当前任务的文件清单）：

```json
{"path":"obj://<tenant>/<authorized-object>","selector":{"symbol":"Cart.total"}}
```

普通 `file_read` 即返回结构块和符号 outline；没有增加独立工具协议。返回的版本、表示和锚点用于追溯原文，临时沙箱路径不会成为公开引用。

## 检索

- 结构代码块直接转换为 LlamaIndex TextNode，不再经过 SentenceSplitter。普通文档沿用原分块策略和 identity。
- 超长外来代码块执行有界行/UTF-8 分割，校验字节长度与引用范围，分片重新计算绝对位置。Knowledge gRPC 的 Struct 数字接受精确整数浮点表示，拒绝布尔、小数、无穷和不安全精度值。
- 代码分块策略参与集合 manifest 和节点 identity；语言、符号与原位置保存到既有元数据/锚点。引用解析仍经实时 ACL 检查。
- 对形如符号名的查询，仅在已授权、已召回的最多 40 个候选中优先精确符号匹配；不将其描述为全仓符号表或保证召回。跨文件引用、类型推断与调用图不在本次范围。

## 验证与交付状态

- 实际 Tree-sitter 语法包运行：Python、JS、TS、TSX、C、Go、Rust，包含嵌套符号、方法、Unicode、BOM、CRLF、大函数、长字符串、严格行选区和输出裁剪。
- 读取到 Memory 索引、搜索与引用解析的本地串联验证通过；确认函数不被二次句切、索引重复请求一致、精确符号优先、跨租户/工作区/用户隔离及撤权。
- 真实 KnowledgeService 与 ModelGateway gRPC 传输验证通过，使用可控嵌入与评分替身；同时验证未携内部凭据被拒和 Struct 数字往返。
- 原 `retrieval_probe.py` 通过，包括索引期间删除后禁止迟到写入。Memory 非生产后端回归 **194 passed、17 deselected**；Qdrant/生产组合未验证，本机没有对应二进制。
- 没有运行商业模型的检索质量评测，不改写 LongMemEval 成绩，不宣称代码检索命中率或 SWE-bench 改善。
- 本次未构建/部署 Linux 文件引擎镜像。上线需要用更新后的锁文件重建镜像、登记新 digest，并同步部署 Memory 的代码块读取逻辑。父分支未完成的生产验收仍然适用。

本地证据见 `evidence/tree-sitter-code-20261110/acceptance.json`。原统一检索验收可按 `evidence/file-engines-m2/README.md` 复跑；本次额外边界检查使用临时脚本执行，遵守 v1 不新增持久测试用例的约定。
