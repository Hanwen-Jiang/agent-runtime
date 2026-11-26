# 开发者 RAG 的代码定义引用

任务是从自然语言里的一个符号跳到 Python 定义。成功要求返回可打开的片段，源文件及声明行都对。11 月 21 日的试验已发现文件在 Top‑8 而证据行仍不在的情况。这里改用仓库现成的测试调用标注定义，并让每个结果走引用解析接口。本轮没有生成回答。[上轮实验](../../resume-eval-loop-20261121/developer-rag/README.md)

## 题单、索引和独立判分

- 固定 Agent Runtime 的 Git 对象 `5af1d890195dda4524634b33fd81beb414467079`。从 `py-core`、`py-memory`、`py-worker`、`py-learning` 四个 Python 包的 `src` 中取不超过 32,000 字节的全部 283 个 Python 文件。以相同的 4,303 个 Tree-sitter 源码块建立 Knowledge 索引。测试文件只用于构造查询，没有进入索引。
- 从测试文件解析直接 `from … import …` 和实际的函数或类调用，在另一份源码 AST 上找对应模块的顶层声明行。先按固定盐与 SHA-256 排序，再按目标文件、测试函数和目标符号各自去重，得到 105 项。排在最前的 24 项用于开发，随后的 48 项用于第一次验收，最后 33 项封存至第二次验收。每道题都有测试路径、调用行、目标路径、声明行和完整查询，题号依次为 `dev-01…24`、`val-01…48` 和 `res-01…33`。[原题单与源码范围](cohort.json) · [33 题新留出](reserve.json)
- 问句由四个中英文模板轮换生成，上下文取测试函数名，用户提供一个反引号标出的符号。查询没有提供目标文件或声明行。同名定义可能有多个，必须定位到测试实际导入的那个。两个测试形式另作说明，`dev-07` 的 `GateRecord` 出现在测试的参数化装饰器中，`val-29` 的 `with_billed_cost` 出现在测试里的 lambda 中，都可从导入语句核实来源。此题单衡量明确的定义导航需求。问题是构造的，未采集真实用户工单。
- 在本机单进程执行真实的 `KnowledgeService.execute(index/search/resolve)`、Tree-sitter、LlamaIndex 和文件权限检查，后端为 SQLite、内存向量库及 64 维开发哈希嵌入。每题并发 1，各方案交错执行 3 次。P95 取所有计时搜索的最近秩，从调用搜索到返回 Top‑8，不含一次性建索引和引用复核。每份 Top‑8 都按返回的绝对字节区间重新读取 Git 文件，核对正文与引用 SHA-256。两轮付费模型调用均为 0。
- 采用条件在验收前冻结。主指标首位证据命中率至少提高 5 个百分点，Top‑8 证据命中数不能下降，搜索 P95 不超过对照的 1.25 倍。裸符号查询、询问行为、多符号查询的结果必须保序，未授权读者须返回零条。[第一轮协议](protocol.json) · [候选与脚本指纹](freeze.json) · [第二轮协议](lookup-protocol.json)

## 第一轮只改变返回顺序

A 使用原检索链，整句恰好是符号时才将已召回的同名定义提前。B 在同样的 40 个已授权候选中，识别明确询问定义且只标出一个符号的问句，沿用原有优先规则。两组召回的原始候选完全相同，普通文本和调用方问题沿用原有行为。

| 划分 | 首位命中 A → B | Top‑8 命中 A → B | MRR@8 A → B | 搜索 P95 A → B |
|---|---:|---:|---:|---:|
| 开发 24 | 2 → 6 | 4 → 6 | 0.111 → 0.250 | 64.3 → 64.7 ms |
| 验收 48 | 1 → 15 | 8 → 16 | 0.061 → 0.317 | 68.2 → 69.0 ms |

48 题中首位新命中 14 道、原首位丢失 0 道。Top‑8 新命中 8 道、原命中丢失 0 道。36 个留出控制请求排序相同，未获文件授权的读者在两组均返回零条。验收通过，保留对显式问句的窄范围优先排序改动。工作区的 Memory 非外部后端测试通过 213 项，另 17 项需要真实后端，未在本轮执行。没有更改线上模型配置。[开发逐题排名](development.json) · [验收逐题排名与耗时](validation.json) · [代码改动](priority.patch)

`val-28` 查找 `dev_executor`，证据原来位于 40 个候选中的第 27 位，B 将它提至第 1 位。`val-02` 查找 `SQLiteRelationalStoreProvider`，两组候选甚至没有包含目标文件。固定模型和问句复放后，48 题中只有 22 题的目标文件进入前 40，只有 16 题的声明行进入前 40。这 16 题 B 均放进了 Top‑8。这是开启第二轮的原因。[原候选池核对](diagnostics.json)

## 第二轮只改变源码定义导航的召回输入

第一轮已有 32/48 道题的定义行落在 40 个候选之外。本轮保持同一索引、嵌入、候选数和 Knowledge 接口。A、B 继续用整句检索。C 在明确的源码定义导航动作中，从问句取出唯一的符号，将它传给已有的裸符号查询入口。两种行为问题与多符号输入继续使用原句。生产通用搜索仍使用完整查询，C 为此实验验证的定向调用方式。

| 划分 | 首引命中 A / B / C | Top‑8 命中 A / B / C | MRR@8 A / B / C | 搜索 P95 A / B / C |
|---|---:|---:|---:|---:|
| 复用 24 道开发题 | 2 / 6 / 14 | 4 / 6 / 14 | 0.111 / 0.250 / 0.583 | 65.4 / 64.4 / 63.5 ms |
| 33 道预留新题 | 1 / 6 / 25 | 4 / 6 / 27 | 0.045 / 0.182 / 0.783 | 65.2 / 66.3 / 63.2 ms |

新留出中，B → C 首引多命中 20 道、丢 1 道，净增 19/33（57.6 个百分点）。Top‑8 新增 21 道，无丢失。27 个控制请求三臂排名完全一致，单独读者未获授权时均返回零条。共比对 792 次返回片段的原始字节和引用哈希（三臂 × 33 题 × Top‑8，包含重复片段）。各臂 P95 有 99 次搜索测量。C 满足预设门槛，适合用在明确的“跳转定义”动作。33 题仍有 8 题排首位错误、6 题连前八都没有正确证据。绝对数来自另一份题单，不能把第一轮 1/48 和第二轮 25/33 拼成增长曲线。[开发24题](lookup-dev.json) · [新留出33题](lookup-validation.json) · [调用差异](symbol_query.py)

在 `res-04` 中，`GrpcRegistry` 的目标是 worker 的 `registry/client.py:82`。整句前八没有正确片段，只传符号后第一个引用就命中。反例 `res-20` 中，相同符号出现在多个评测桥接模块。去掉与题意有关的上下文后，正确的 `deepeval_metrics.py:141` 从第一降到第三。下一步优先验证用调用方模块消歧，并收集脱敏的实际开发者问句和线上嵌入服务的结果，再考虑扩大使用范围。

## 复跑

从 agent-runtime 根目录运行。为避免干扰其它工作，可以用独立的 `UV_PROJECT_ENVIRONMENT` 安装仓库锁定的 Memory 测试依赖，并按文件引擎锁文件补装 Python 语法包。

```sh
export UV_PROJECT_ENVIRONMENT=/tmp/cgent-rag-eval-venv
uv sync --frozen --package agentruntime-memory --extra test
uv pip install --python "$UV_PROJECT_ENVIRONMENT/bin/python" 'tree-sitter==0.26.0' 'tree-sitter-python==0.25.0'
export PYTHONPATH=services/py-worker/src
D=docs/plan/evidence/resume-exp-20261123/developer-rag
"$UV_PROJECT_ENVIRONMENT/bin/python" "$D/evaluate.py" validation /tmp/code-nav-48.json
"$UV_PROJECT_ENVIRONMENT/bin/python" "$D/symbol_query.py" reserve /tmp/code-nav-33.json
```

运行时 A 从冻结 Git 对象读取原排序函数，B 使用当前 Memory 源码。复现本次 B 须核对 `freeze.json` 中的 Memory 文件 SHA（或在对应基础提交应用 `priority.patch`）。脚本拒绝覆盖已有结果。计时随机器负载变化，排名可逐题复核。
