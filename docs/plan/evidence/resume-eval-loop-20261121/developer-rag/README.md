# 开发者实现定位：冻结题单的检索试跑

**结论。** 只在源码中检索，在 12 道验证题上将 Top‑8 中包含正确文件的问题从 3 道增至 7 道，但包含预标证据行的问题仍为 2 道。此次不把源码范围设为默认，也不宣称回答正确率提高。查询来自本轮助手拟定的维护者视角问题，证据锚点未经人工确认。

| 数据划分与配置 | Gold 文件进入前 8 个候选段 | Gold 文件和预标行均在前 8 段（主指标） |
| --- | --- | --- |
| 开发 6，原始 TF-IDF | 2/6 | 0/6 |
| 开发 6，同一检索器增加符号子词 | 2/6 | 0/6 |
| 开发 6，切换到只索引源码 | 3/6 | 3/6 |
| 冻结验证 12，原始 TF-IDF | 3/12 | 2/12 |
| 冻结验证 12，切换到只索引源码 | 7/12 | 2/12 |

**如何选题。** 先将 18 道中文问题、各自功能组、单一参考文件与行号冻结为 [cohort.json](cohort.json)，SHA‑256 为 `c6c52bc214489c96767ab6743b6631634afe5c972191ea01f80bce59e8bf98ac`。前 6 道用于开发，覆盖 webhook 收件箱、会话事件、缓存连接、模型结算、上下文恢复和模型固定。另 12 道来自不同功能组，包含登录、PR、预览、定时、邮件、工作区、沙箱、环境识别、代码检索、SWE 抽样、重复评测及 Reviewer。没有随机种子；两组原题和功能名均不重叠。同一作者编题及标注，可能存在用词与已知代码的关联；本轮 18 题全是“实现在哪里”的可回答题。

**如何选方案。** 第一轮预设的标识符子词扩展未增加命中。看开发集前几条后发现，架构与运维文档经常排在源码前，于是增加一个开发候选：对实现定位类查询限定源码范围，排除 Markdown，再用同一个 TF-IDF 检索器。按开发集主指标选择了这一项。[候选冻结记录](candidate-freeze.json)包含脚本指纹和选定理由；冻结完成后才对验证集的两种配置做一次对照。

**语料与判分。** 使用 `agent-runtime@da58416909fc54f02519de95706df01d9bbd1701` 和 `coding-agent@0cd2c890d214a53a3768568b7d80103eb0fd7341` 的 Git 对象。保留指定源目录的 Python、Go、Rust、TS/TSX、Markdown 和 README，仅取不超过 128 KiB 的文本文件，排除测试、生成文件、历史证据等，实际语料 837 文件。按 64 行分段、步长 48：基线 3,799 段，源码范围 3,302 段。每题取前 8 个段落，文件命中和预标行命中分别计数。没有生成回答，也没有人工复审排名前列的替代证据。仅测本地词法召回，无法据此外推生产混合检索、端到端问答或真实用户流量。

**失败案例与下一项验证。** 验证题 `v10` 要求定位按仓库及难度选择 SWE 子集的实现。源码范围方案把正确的 `swebench.py` 排在首位，但返回了文件开头，而预标的 `stratified` 从第 154 行开始。另有 `v3/v4/v11` 也从“不见文件”变成“命中文件但无证据”。下一次可在初召回文件内部按函数或方法再选段，用另一份人工核验的真实提问集测引用行覆盖，并允许多个等价锚点。当前 12 题保留原结果。

原始逐题 Top‑8 与命中状态：[开发：子词](development.json)、[开发：源码范围](development-scope.json)、[验证](validation.json)。文件含逐题本地查询用时，仅供诊断；未控制重复测量与机器负载，不作为时延 SLA。

## 第二轮：在召回文件内重新找证据行

**待验证的问题。** 前一轮召回了更多正确文件，返回段落却常停留在文件开头。第二轮以第一轮的源码范围方案作为对照。新候选先按原词法得分取得一批不同的源文件，再按声明位置划分方法或类型段落，每段不超过 64 行；对查询和段落做中文单字/双字及英文标识符分词，用 BM25 排序，每个文件最多保留两段。检索接口只接收自然语言问题和语料，不读取金标文件或行号。

**先冻结题目，再选参数。** [新增验收清单](cohort-round2.json)有 10 个新问题，涉及元数据锁、产物下载、告警接入、仓库枚举、Temporal 大载荷等功能，其金标文件与原 18 题均不相同。这批题仍为助手拟定和标注，未经人工复核。只在原 6 个开发题对比候选文件数 6 与 16，证据行分别命中 0 与 1 题，均低于原方案的 3 题。冻结相对较好的 16 文件候选后，对已看过的 12 题仅做回归，在新 10 题验证一次。随后停止调参。[开发记录](round2-development.json) · [候选与脚本指纹](round2-candidate-freeze.json)

| 题组 | 对照 → 两阶段：文件命中数 @8 | 对照 → 两阶段：证据行命中数 @8 |
| --- | --- | --- |
| 原开发 6 道 | 3 → 4 | 3 → 1 |
| 原 12 道，现仅用作回归 | 7 → 6 | 2 → 2 |
| 新验收 10 道 | 5 → 6 | 1 → 2 |

**去留决定。** 原来的 `v10` 从只找对文件变为在第 2 个候选找对函数行，但同批 `v1` 丢掉了原本命中的 OAuth 行。新 10 题中 `h8`、`h9` 新命中，`h7` 退步，净增一题。由于绝对命中仍低、开发集退步，而且只有单一助手标注的行作为唯一参考，不替换线上默认。接下来应由维护者核实可接受证据区间及等价来源，补齐真实使用问题，再决定是否继续投入。本轮没有进行模型推理或线上配置修改。

[原 12 题回归](round2-regression.json)与[新 10 题验收](round2-fresh.json)保留每题的候选位置。要复算，用同一环境执行：

```sh
d=docs/plan/evidence/resume-eval-loop-20261121/developer-rag
.venv/bin/python "$d/evaluate_symbols.py" --repos .. --split development --file-pool 6 --file-pool 16 --out /tmp/symbols-dev.json
.venv/bin/python "$d/evaluate_symbols.py" --repos .. --split validation --file-pool 16 --out /tmp/symbols-regression.json
.venv/bin/python "$d/evaluate_symbols.py" --repos .. --split fresh --file-pool 16 --out /tmp/symbols-fresh.json
```

**复算。** 使用 `agent-runtime` 项目 Python 环境、两个仓库的同一父目录，在项目根目录执行。首次开发产物保留了原有字段，当前脚本还输出可检索段数；排名与指标定义一致。

```sh
d=docs/plan/evidence/resume-eval-loop-20261121/developer-rag
.venv/bin/python "$d/evaluate.py" --repos .. --split development --candidate identifier_subwords --out /tmp/rag-dev-subwords.json
.venv/bin/python "$d/evaluate.py" --repos .. --split development --candidate implementation_scope --out /tmp/rag-dev-scope.json
.venv/bin/python "$d/evaluate.py" --repos .. --split validation --candidate implementation_scope --out /tmp/rag-val-scope.json
```
