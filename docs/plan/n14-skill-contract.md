# N-14 用户 Skill 的契约草案

> 状态：契约 `c1836ba`（S-01），2026-11-04 人审通过（用户在对话里：“Approve all three”；“代审时要明确的”五条照草案）。`buf lint`、`buf breaking` 通过，生成代码已更新，契约测试 599 过；兼容性基线已更新（含本契约）。
> 依据：[feedback 第 4 节](feedback-2026-11-01.md)（租户登记名称、说明、文件，Planner 的目录列出，tool_loop 按需读入，走 Registry 的新种类）；[代审结论](delegated-review-2026-11-04.md)与[后续](delegated-review-followup-2026-11-04.md)的 N-14 行（不可变版本，用户只能扩展平台，Planner 只拿目录，tool_loop 按需读已选版本的正文；验收：v2 发布后固定 v1 的仍读 v1、用户同名不能遮蔽平台、非授权主体读不到正文）；[配置包前置基线](skills-config-package-baseline-2026-11-02.md)（D-27 不替代本契约）。

## 草案

| 部分 | 草案 |
| --- | --- |
| 种类 | `RegistryKind` 加 `REGISTRY_KIND_SKILL = 7`；正文按新的 `schemas/skill.schema.json`。名字：小写字母开头，小写字母、数字、`_` 与 `-`，最多 64 个字符 |
| 三层 | 平台（Registry 的 Publish 不带租户）、租户（Publish 带租户；两者都要发布令牌，即只由运维发布）、用户（新的公开服务 `skill.v1.SkillService`，只有登记者本人看得到）。`SkillScope`：PLATFORM、TENANT、USER |
| 同名 | 上层优先：平台 > 租户 > 用户。下层不能发布上层已有的名字（Conflict，`name_taken`）；上层之后才有的同名条目遮住下层的（`shadowed`，不进目录、不被新 Task 固定，已固定它的 Task 照旧读） |
| 版本 | 每层里按发布顺序 v1、v2……，一经发布不再修改；发布即成为当前版本（DEFAULT），上一个变为 RETIRED，没有影子、不走 Promote。正文与当前版本相同（规范化 JSON 的 SHA-256，`digest`）时不产生新版本 |
| 固定 | `PinnedVersions.skills`（字段 6）：名字 → `platform/vN`、`tenant/vN` 或 `user/vN`（user 是 Task 的提交者）。提交时看得到的当前版本全部固定；之后的发布、收回、遮住都不影响这个 Task。带层的写法让同名的上层条目之后出现时，已固定的仍指向原来那一层 |
| 内部读 | `RegistryService.GetSkill`（正文；按固定的版本照原样取回，含 RETIRED 与被遮住的）、`ListSkills`（目录：名字、版本、层、说明，不带正文；带固定版本时只列固定的）。看得到的范围按 `TenantContext`：平台层、`tenant_id` 的租户层、`user_id` 本人的用户层；别人的用户层与不存在的一样 NotFound（`skill_not_found`） |
| 用户的接口 | `SkillService`（与 Task API 同一个端口，经 Ingress 认证；用户与租户取自认证身份，请求里的 `tenant_id` 只校验一致；带 `task.approve` 也不能动别人的）：`PutSkill`（发布下一版；`created`、`new_version`）、`ListSkills`（看得到的当前版本，本人被遮住的也列出）、`GetSkill`、`RetireSkill`（幂等；收回后不进目录、不被固定，已固定的照旧，再发布接着编号） |
| 正文 | `description`（1～1024，目录只看它）、`instructions`（Markdown，1～64 Ki 字符）、`files`（最多 20 份文本：`path` 相对、/ 分段、每段不以 . 开头，`media_type` 只收五种文本类型，`content` 最多 64 Ki 字符）。不能声明 effect、联网、后端、凭据、Hook 或可执行的东西（`unevaluatedProperties: false`）。Registry 另查：路径不分大小写不重复、不能一个是另一个的前缀目录，整份正文按 UTF-8 不超过 256 KiB |
| 限额 | 每个用户最多 100 个没收回的 Skill（ResourceExhausted，`skill_limit`）；部署的默认值，不进租户策略 |
| 错误码 | `name_taken`、`skill_limit`、`invalid_document`、`skill_too_large`、`duplicate_path`、`invalid_name`、`skill_not_found`、`pinned_skill_not_found` |
| 保密 | 正文只进 Registry 与读它的 Task：日志、trace、审计里没有正文；审计只记（租户，用户，name，版本，动作，时间） |

## 实现时怎么接（不改契约）

- task-service：Registry 的 `kinds` 加 SKILL（`eitherScope`，发布即生效）；用户层存在租户作用域里、名字带登记者前缀（表结构不变）；`resolve` 固定看得到的当前 Skill；`SkillService` 挂在 Task API 的端口上，经 Ingress 认证后调 Registry 的内部方法。
- Planner：在现有的规划 activity 里按 `pinned_versions` 调 `ListSkills`，目录加一节“Skill：名字 — 说明”；不新增 activity，不改命令序列，不要版本分支。
- tool_loop：Task 固定了 Skill 时登记 `skill_read`（effect read，同 `context_read` 在 Worker 进程内）：参数 `name` 与可选的 `path`，返回固定版本的 `instructions` 或那份文件；没固定的名字报 `skill_not_pinned`。结果与别的工具结果一样进 history 区，超过阈值照旧转存。span `execute_tool skill_read` 记名字与版本，不记正文。
- Skill 的文本只是给模型的说明：effect 上限、租户策略、审批照旧，Skill 写了什么都不改变权限。
- coding-agent：设置里加“我的 Skill”（列出、编辑、收回），经 `SkillService`；Runtime 回 Unimplemented 时不显示。

## 验收（实现时先写成测试）

1. 发布 v2 后，固定 v1 的 Task 经 `GetSkill`、`skill_read` 读到的仍是 v1；新 Task 固定 v2。
2. 用户层发布与平台层、租户层同名的 → `name_taken`；平台之后发布同名的 → 用户的 `shadowed`，新 Task 固定平台的，已固定用户层的 Task 照旧读用户层。
3. 别的租户、同租户的别的用户读某人的用户层 Skill → NotFound，与不存在的一样；缺租户 denied；`ListSkills` 不带正文。
4. schema 正反例（`schemas/examples/skill.examples.json`）；Registry 另查的三条各有反例。
5. 正文不变再发布 → 不产生新版本，`digest` 相同；改一个字 → 新版本。
6. 收回后不进目录、不被固定；已固定的照旧；再发布从下一个号接着编。
7. 第 101 个 → `skill_limit`；收回一个之后可以再发布。
8. 日志、trace、审计里找不到正文里的标记串。
9. Task 工作流的 golden 历史照旧回放（只多一个 `PinnedVersions` 字段，命令序列不变）。

## 代审时要明确的

1. 用户层只有本人看得到；租户里共享的由运维经 Registry 发布。同租户里按人共享另立 ACL（与 D-27 的 PRIVATE 首版一致）。
2. 名字收 `-`（Node Template、Recipe 的名字不收），方便沿用常见的 Skill 名。
3. 不走 Promote：用户的 Skill 不评测，平台层的也发布即生效。
4. 一个 Task 固定看得到的全部当前 Skill（不是只固定 Planner 选中的），这样 tool_loop 中途用到哪一个都有固定的版本。
5. `files` 只经 `skill_read` 读，不放进工作区或沙箱；要放进沙箱的是 D-27 的配置包。

## 复杂度决策

| 项目 | Failure | Minimum fix | Benefit | Cost | New failure surface | Decision |
| --- | --- | --- | --- | --- | --- | --- |
| 带层的固定值（`user/v2`） | 上层之后出现同名条目，已固定的 Task 读错层 | 固定值写明层 | 已固定的 Task 可复现 | 解析多一步 | 旧客户端只认 vN | KEEP |
| 影子与 Promote | 用户 Skill 没有评测集 | 发布即生效 | 少一套状态 | 无 | 坏的 Skill 立即生效（只影响本人的 Task） | REJECT |
| 正文进 Planner 的目录 | 目录变长、提示被注入的面变大 | 目录只放说明，正文按需读 | 省上下文 | 多一次工具调用 | 模型不读正文 | KEEP |
| 文件交付到沙箱 | 与 D-27 的配置包重复 | 只经 `skill_read` | 少一条交付路径 | 无 | 用户以为文件在工作区里 | DEFER（D-27） |
| 可执行脚本 | 包里的脚本绕过审批 | 只收文本，不执行 | 首版攻击面小 | 无 | 无 | REJECT |

## 实现（N-14-IMPL，2026-11-04）

| 部分 | 做法 |
| --- | --- |
| Registry（`internal/registry/skills.go`） | 三层都在 `registry_entries`，表结构不变：用户层在租户作用域、名字存成 `~<user_id>/<名字>`。同名时上层优先（下层发布上层当前有的名字是 Conflict `name_taken`；上层后来才有的遮住下层的，`shadowed`）。发布即生效，正文与当前版本相同时返回当前版本。`ResolvePinnedVersions` 固定看得到、没被遮住的当前版本。用户层每人 100 个当前的（`registry.ErrSkillLimit`；查数与写入不在同一事务，并发时可能多出一两个）。种子文件收 `kind: skill` |
| 固定的版本给 Worker | task-service 提交时把 `PinnedVersions.skills` 另记在 `tasks.skill_pins`（taskstore 迁移 8，JSON）。Worker 按 task_id 读它与 `submitted_by`，不经工作流，命令序列不变；没有这行（sub_task 的子 Task）或旧库没有这一列时当作没有 Skill |
| `SkillService`（`internal/skillapi`） | 挂在 Task API 的端口上，经 Ingress 认证与限流；没配 Registry 时 Unimplemented（`skills_unavailable`）；审计只记租户、用户、名字、版本、动作 |
| Planner | `RegistryContextSource` 按固定的版本经 `ListSkills` 取名字与说明，放进 `CatalogView.skills`；规划的 brief 列出它们（是数据，不进 system 区）。有 Skill 时 `_with_reads` 给 tool_loop 的 Goal Node 加上 `skill_read`；没有 Skill 的 Task 不把 `skill_read` 列进目录 |
| `skill_read`（`agentruntime_worker.tools.skills`） | 设了 `AR_REGISTRY_ADDRESS` 才登记，effect read：不给 path 时返回说明、instructions 与文件清单，给 path 时返回那份文件；没固定的名字 `skill_not_pinned`，没有的文件 `skill_file_not_found`；用户层按提交者读 |

验收第 1～8 条有测试：`internal/registry/skills_test.go`（两种后端）、`internal/skillapi/server_test.go`、`internal/taskapi/skill_pins_test.go`、`services/py-worker/tests/test_skills.py`。第 9 条（golden 历史照旧回放）：Worker 的工作流没有改动。

没做的：coding-agent 的“我的 Skill”（另一个仓库，下一步）；端到端用例（task-service + Worker + Registry 真跑一次 skill_read）；租户层的收回（Registry 没有收回的方法，平台层、租户层由运维发新版本）。
