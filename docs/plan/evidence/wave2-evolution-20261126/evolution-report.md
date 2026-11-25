# Wave 2 Self-Evolution Cycle Report

- Status: `pending_approval`
- Isolated Tenant: `sys-evolution`
- Candidate ID: `None`
- Staged Registry Skill: ``
- Matrix Gate Passed: `True`

## 1. Failure Clusters

- **readonly_churn** -> `explore-repo` (support=3, families=['coding.bugfix', 'coding.feature', 'coding.refactor']): excessive read-only steps / read_only_reminder triggered before targeted edits

## 2. Proposed Diff (`explore-repo`)

- Rationale: 针对 readonly_churn（support=3）：excessive read-only steps / read_only_reminder triggered before targeted edits

```diff
--- a/skills/explore-repo/instructions.md
+++ b/skills/explore-repo/instructions.md
@@ -5,3 +5,6 @@
 2. 精准搜索符号：`repo_grep` 支持 `glob` 与 `context_lines`，定位类/函数定义及其全部调用点。
 3. 先看轮廓再读细节：先用 `file_inspect` 看文件行数、语言和顶层符号，再用 `repo_read(with_line_numbers=True, start_line=..., end_line=...)` 只读目标区间。
 4. 产出调用链摘要：明确记录模块边界、相关单测命令与潜在影响面，禁止猜测未阅读的文件内容。
+
+【自进化补充检查项（针对聚类：readonly_churn）】
+- 高效定位防盲读：先使用 `file_inspect` 看行数与符号索引，再以 `repo_read(with_line_numbers=true, start_line=..., max_lines=...)` 先定位再切片；避免连续全文件盲读超过 2 轮仍不收敛。
```

## 3. Baseline vs Candidate Matrix Gate

- Mode: `fixture` (`is_offline_fixture=True`)
- Pass rate: baseline `0.6667` -> candidate `1.0` (delta `0.3333`)
- Blind read ratio: `0.0909` -> `0.0`
- Tool error rate: `0.0` -> `0.0`
