# N-04 Task Plan 契约候选基线

> 状态：候选基线，未修改 `/schemas`、`/proto` 或生成代码。该文件只供 S-01 人审和后续验收实现使用。

## 首版候选行为

- Planner 在非 fast 模式的第一轮产出 3～8 个步骤；每步包含稳定 `id`、用户语言的 `title`、`status` 和所属 `round`。
- `status` 首版候选值为 `pending`、`active`、`done`、`blocked`；同一时刻最多一个 `active` 步骤。
- `plan_updated` 事件携带完整步骤快照、当前步骤 id 和 `round_seq`，客户端可丢弃旧快照后重建计划区。
- Context Builder 将完整计划放在 brief 的固定区；压缩观察、历史摘要或重新规划不能删除计划步骤。
- fast 模式不生成计划，也不发 `plan_updated`；旧客户端忽略未知字段和事件后仍能消费现有事件流。

## 验收边界

验收 fixture 覆盖：3～8 步边界、非法空步骤、状态转移、round 归属、事件完整快照、计划在压缩后保留、fast 不生成、旧客户端兼容。fixture 只描述预期，不宣称当前运行时代码已经支持这些字段。

## 复杂度决策

| 项目 | Failure | Evidence | Minimum fix | Benefit | Performance cost | Implementation cost | Operational cost | New failure surface | Release slice | Decision |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 计划快照事件 | 前端只收到局部更新，重连后计划不完整 | SSE 支持 Last-Event-ID，计划字段尚不存在 | 一个完整 `plan_updated` payload | 重连和旧客户端恢复简单 | 每轮一次小快照 | 中，需 schema/event review | 事件体略增大 | 步骤状态落后 | S-01/N-04 | KEEP |
| 第二套计划持久化表 | 计划与 Task State 分裂 | Task State 已是工作流权威状态 | 复用 Task State + 事件快照 | 少一个状态源 | 无额外读写 | 低 | 无新迁移 | 事件与 State 不一致 | N-04 | REJECT |
| 计划版本/哈希 | 客户端收到乱序更新 | 现有事件 id 已提供顺序 | 复用 `event_id` 与 `round_seq` | 不增加新 generation/hash | 无额外计算 | 低 | 无新字段运维 | 同 round 重放需去重 | N-04 | DEFER |

任何字段编号、事件枚举或 schema 变化都必须先补正反例，再执行 S-01 的 buf/schema 检查和人工评审。
