# SWE-bench Verified Mini 50：冻结题单与配对口径

此目录保存本轮端到端实验输入。此文档的就绪检查、协议和模拟单测均不是模型成绩。正式结果须由对应运行归档输出。

## 当前正式运行口径（2026-11-21）

正式两臂改用 `a6api/gpt-6.2-sol@2026-11`，`strong@medium`，thinking budget `8192`。`gpt-6.1-sol` 只作预检，不能进入正式成绩。该 Profile 是隔离目录中新生成的不可变版本，沿用 6.1 的价格字段作为预算占位，实际价格以网关 `model_usage` 账本为准。网关启动、Registry revision 和两臂工作区预检已通过。

正式 50+50 已启动但尚未提交模型 Task。每题 readiness gate 需要数据服务和项目环境 builder。运行期间节点出现真实 `disk-pressure`，PG Pending，环境预热结果未 ready，采集器已停止，未把等待或错误计作模型失败。基础设施恢复后，继续复用本文件的题单与协议。

6.2 首次正式尝试的两个臂各产生 3 条 prewarm 记录，均 `ready=false`，没有 `task_id`、模型调用、补丁或计分。该尝试只作为基础设施失败归档。

两道 Sphinx 的可复现入口是 [`run_sphinx_gpt62.py`](run_sphinx_gpt62.py)，固定 [`sphinx.ids`](sphinx.ids) 中的 `sphinx-doc__sphinx-8056` 与 `sphinx-doc__sphinx-9367`。它复用 Mini 50 的逐题只读项目镜像 prewarm，使用同一 GPT‑6.2 Profile 与同一官方评分器，协议明确标记 `mini50_denominator=false`。这两题用于先验证工作区、环境和完整 Task 链路，不能替代 50 题分母，也不能把旧 GPT‑6.1 的结果混入新模型成绩。

## 题单是谁选的？

使用 Marius Hobbhahn 的社区 Verified Mini，固定上游提交 `1ce432065d9ed226017ef1ac88b5f91818383c64` 的 [size_optimized_sample_ids.json](https://github.com/mariushobbhahn/SWEBench-verified-mini/blob/1ce432065d9ed226017ef1ac88b5f91818383c64/data/subsets/size_optimized_sample_ids.json)。作者从 Verified 500 题中给既有模型表现、测试通过与难度分布做聚类，并优化镜像存储，结果为 Django 25 题、Sphinx 25 题。这是一份公开、固定、低成本清单，不是官方另设的 Verified-50 分区。[上游方法](https://github.com/mariushobbhahn/SWEBench-verified-mini/tree/1ce432065d9ed226017ef1ac88b5f91818383c64)

- [上游题号原字节](upstream-mini50-ids.json) SHA‑256 `04c17d83724b3fd0c6107bdbbd78f1e8679d26e7f7e3fb86f771ed5ba0926819`；[运行 CLI 输入](mini50.ids)按相同顺序每行一个实例。用 `--ids`，不要用 `--limit 50` 或重新抽样。
- [逐题仓库、base_commit、难度、初次本机镜像检查、两个分组完整 ID](cohort.json)。服务器上 500 道 Verified 原始导出的 SHA‑256 为 `2d336d3f60d2a6496b21cf04ab73ed6cdd016eb52f19e1e4bb0328a278145d52`，Mini 全 50 题都存在，入选的人工估时档依次为 19 道少于 15 分钟、23 道 15 分钟到 1 小时、7 道 1–4 小时、1 道超过 4 小时。元数据投影不包含题解、隐藏测试和官方补丁。
- 和旧运行 `t18-rev19` 清单重合 6 道：Django 11885、11951、12262；Sphinx 8056、9367、10673。本轮启动冒烟两道都在这 6 道内。另有 44 道仅能称“相对于旧 50 清单新”，还应核对其它历史调试使用情况。原开发集分数不能当作这 44 题的参考标签。
- 初查时，官方评分容器的本地 Docker 缓存只有 7/50；[逐镜像公开 digest 与大小](grader-image-inventory.json)和[共享层核算](grader-layer-inventory.json)区分每标签总和与实际唯一压缩层。43 张缺失镜像逐 tag 标称约 45.23 GiB，但相对缓存的七份只另需约 6.50 GiB 压缩层，不含解包和运行耗用。预取脚本按公开 manifest digest 限单并发，并检查主机磁盘。[预取实现](prefetch_graders.py)
- **Docker grader 缓存不等于 Runtime 就绪。** [部署原始状态](initial-runtime-envs.json)中只有七个 Mini 题有历史转换记录，七条均为 deleted。CRI 导入也不等于本次会话选中该环境。两道冒烟需先能真实克隆至对应公开 base_commit、获得快照、在正常服务入口生成补丁，再经官方容器判断；缺少这一门槛时不能报告正式50的得分。

## 怎样运行与读出结果？

旧版本和候选分别创建独立的原生 run 目录，采用同一份 `mini50.ids`、相同判分器与镜像来源、无 hints。预先记录实际新模型/档位、最大推理预算、工具权限、沙箱资源、单题与总费用上限、并发、超时和重试。保留旧版本与本轮仅变动 Reviewer 验证证据的候选版本/镜像指纹。公开基线到候选不得以官方测试、参考补丁或本轮 44 题的结果反向修改候选。记录成本是模型费用、仅已结算挂账还是包含基础设施费用；未知总额不填零。

每题只记一次正式生成，运行内的思考、工具调用和模型轮次可多次。整题重新启动、重试生成与模型回退需另计。每臂从实际模型账本按 run → task → case 汇总为 `model-audit.json`：

```text
run_id                     对应原生 run.json
runtime_revision           实际参与运行的版本，含必要镜像或源码摘要
model_profile, tier        预先固定的新模型与推理档
settings_digest            同一不可变推理/工具/预算/重试协议的 SHA-256
items[instance_id]         generation_attempts, model_calls, actual_profiles, actual_tiers, audit_complete
```

`actual_profiles` 和 `actual_tiers` 是各实际 profile 或推理档到调用数的映射。仅有运行标签或计划 profile 的记录不足以确认模型身份。相同道理，选项写 official 不等于实际使用了官方执行环境；需原生逐题 environment_used=true，或独立验证的 official_environment_verified=true，或来源完整的 audited_shell_calls=0（本题未调用 shell）。嵌入、评分器及模型内部多轮调用与整题生成重试分别统计。

```sh
python3 compare.py BASELINE_RUN_DIR CANDIDATE_RUN_DIR \
  --expected-model ACTUAL_MODEL_PROFILE --expected-tier ACTUAL_TIER --output paired-results.json
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s . -p test_compare.py
```

[汇总器](compare.py)接受每臂的原生 run.json、report.json 和独立导出的 model-audit.json。逐题区分 resolved、unresolved、empty_patch、run_error、grading error、ambiguous failure、尚未运行/判分，以及审计缺失、污染或多次生成。分别披露公开50、相对旧集新44、重合6、冒烟重合2的分母、已观察到的官方解决数及覆盖。数字为 null 表示缺失，不作零处理。只有协议匹配、两臂版本不同、该分组每道都有明确官方判定和首试模型审计时输出正式差值。否则只输出可观察信息与缺口；汇总器退出码 3 表示这批正式证据还不完整，2 表示输入不合法，0 才表示全部 50 对可比较。任何外来题号或重复行会拒绝输入而不会静默覆盖。

## 面试复盘应回填的事实

1. 故障最早发生在哪个操作，基线表现与修后同一协议的 HEAD、快照、Task 和模型调用是否齐全。
2. 50 题、44 题、6 题分别有几对有效：两版均过、新过、丢失、均未解决、无效或未判；写运行编号、分母和含重试成本。
3. 选一例新过和一例退步，用公开题意、实际执行轨迹和官方最终判分解释；区分单变量证据与合理假设。
4. 依据预设门槛作采用、继续验证或淘汰决定。此前研究可核对 [mini-SWE-agent 官方同题重算](../../../../../8-legged/AI全栈备考/实验复盘/SWE与Reviewer.md)，不同模型/预算或重试设置不能直接归因为 Runtime 改动。
