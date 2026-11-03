# SWE-bench Verified 改编试写（2026-11-03，未确认）

本目录有 **4 条 S-09 分诊/规划草稿、2 条交付问答草稿**，来自 4 道真实原题。
新增标签和问答参考答案均为 AI 起草，未冒充人审；没有改动已确认的 `task-loop`，没有运行模型或发布能力分。

## 来源与选择

来源是 [princeton-nlp/SWE-bench_Verified](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified)，
固定 revision `c104f840cc67f8b6eec6f759ebc8b2693d585d4a` 的 `data/test-00000-of-00001.parquet`。
通过固定 revision URL 下载并读取原始 Parquet；`upstream_rows.jsonl` 保留所选四行全部原字段和值，
包括原题、gold patch、test patch、FAIL_TO_PASS、PASS_TO_PASS 和 base_commit。
`sources.jsonl` 按本地 case ID 记录来源、字段归属与分组。

| 原题 ID | 选题理由 | 本地用途 |
| --- | --- | --- |
| astropy__astropy-12907 | 嵌套模型依赖矩阵；小补丁但需要理解矩阵语义 | 规划、补丁问答 |
| django__django-16082 | 混合数值类型的 MOD 表达式；覆盖 Web/ORM | 规划 |
| psf__requests-2931 | 请求体字节与 URL 参数的处理边界 | 规划、补丁问答 |
| sympy__sympy-16886 | 短小确定的数据映射修复；难度较低但仍需写工作区 | 规划 |

这是便于逐条核对的目的性试写样本，不是随机抽样，也不代表完整 SWE-bench 的难度分布。
全部归入本地 `dev`；官方 split 保留为 `test`。同一道原题的规划与问答共享 `group_id`，
以后划分 dev/holdout 时不得拆开；本批已经阅读和展示，不能再宣称为未见留出集。

## 标签与输入边界

4 条均标 `full`：新的仓库修改任务，无匹配 Recipe，授权风险从 read 上升到 write_reversible。
简单的 Morse 映射修复也不能仅因代码改动少就标 `light`。当前批次没有 fast/light/replan 覆盖。
`shape` 只约束 tool_loop、单写者和交付；不把 gold patch 的具体实现写进 Planner 输入。
`reference` 是一种可接受 Stage，不是官方规划答案。参照中的 revise 是测试验证的一种组织方式，
不是必选形状；tool_loop 内直接运行相关测试同样可以满足任务要求。

Planner 输入保留原始 `problem_statement`，外层说明 repo/base_commit 与评测场景的工作区前提。
这个组件试写不会创建或执行真实上游仓库。gold patch、test patch、FAIL_TO_PASS、PASS_TO_PASS、hints_text
保存在离线来源文件；补丁和测试 ID 也用于下面的合成检查点，不能整份加载进 Planner 的消息或工具可见工作区。

现有 TaskCase schema 要求至少一个 Round，因此每题附一个**人工合成、从未执行的成功检查点**，
并在 description/review/result 中说明。检查点给出原始 gold patch、test patch 和逐题具体修复说明；
测试结果明确为场景假设，列出官方 FAIL_TO_PASS/PASS_TO_PASS ID，并标记 `actually_executed: false`。
`done` 只在“这些测试均已通过”的虚构前提下成立，不是本次运行得出的事实。
这些 Round 只供 schema/参照输出检查，**不得计入 S-10 能力样本**。
尤其不要把此目录传给默认收集 Task Round 的 `s10` 适配器。

S-09 本身只评估分诊、规则和 Stage 形状；保存官方答案不等于已经评估修复正确率。
真正的 SWE-bench resolved rate 仍须原题环境和官方测试 harness；本次没有运行。

## 仓库问答

`repository_qa.jsonl` 的 2 条遵循 deliverables 的 `id/message/expected_output/metrics/confirmed` 字段形状。
它们明确提供实现补丁和测试补丁作为阅读材料，再提出新写的问题。这里 patch 是被询问的材料，
不是要对修复模型隐藏的答案；不要将这两种任务的输入规则混用。
参考答案针对补丁里的可见事实：Astropy 的矩阵块拷贝、Requests 的 body/params 分工及测试断言边界。
没有声称实际 checkout 或测试成功，不能称为完整仓库探索测评。

这两条仍是待确认的数据文件，未接入现有 DeepEval fixture runner 或能力矩阵；后续需明确消费文件和计分入口。
若扩展到真正仓库问答，应固定仓库 revision 并提供实际上下文，不能沿用本批输入片段的覆盖结论。

## 来源声明

原题与补丁归上游作者。本地问题包装、分诊标签、Stage 和问答参考答案属于此次改编。
`licenses/` 原样保存四个仓库在对应 base_commit 下的 LICENSE 文件：Astropy、Django、SymPy 为 BSD 许可文本，
Requests 为 Apache 2.0 声明。数据集卡当前没有单独声明数据集 SPDX license，不能把 SWE-bench 评测代码的许可
自动当成全部 issue 文字的授权声明。保留原始归属和许可；本目录未新增全量数据镜像。

## 已做的离线校验

使用现有 `load_eval_set` 加载 4 条：schema、ID、DAG、Catalog 引用均通过，4 条全部仍为未确认。
将各自 reference 交给现有 `score_planner` 和 `score_reviewer`，4 条规划与 4 个合成检查点均通过。
校验原题与 Planner 消息尾部逐字一致、来源 ID 唯一、问答引用的 patch/test_patch 来自对应原行。
这是结构和来源自检，不是模型能力评测，也不是人工确认。
