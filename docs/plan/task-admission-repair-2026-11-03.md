# Task 准入实现修复

本次不改两项已批准行为：每租户最多两个未终态 Task；重复提交返回原 Task。

- **Failure**：Task Service 无法编译；修复编译后 PostgreSQL 拒绝直接引用 tenant_id 的准入锁 SQL。
- **Evidence**：go test 编译报 QueryOne 三个返回值被两个变量接收；Postgres prepare 明确禁止应用 SQL 访问租户列。
- **Minimum fix**：接收 QueryOne 的 found 值；锁只使用作用域内 lock_id，租户隔离交给已有 RelationalStore。
- **Benefit**：恢复服务编译与跨后端一致的准入检查。
- **Performance cost**：调用次数不变。
- **Implementation cost**：增加版本 6 迁移及准入、幂等、隔离、升级回归用例；effect 测试完成一个 Task 后再提交第三个，避免混入并发上限。
- **Operational cost**：部署时重建不含业务数据的锁表；sessions/tasks 保留。
- **New failure surface**：迁移需要已有业务库迁移权限，无新服务。
- **Release slice**：独立修复，预算接线前。
- **Decision**：KEEP，修复可复现错误，不扩展冻结契约。

验证：Task Store SQLite 用例及 Task API 测试通过。随后在 WSL Debian 找到 PostgreSQL 17.10，用 Go 1.26.5 执行 Task Store、Task API、Registry 与 task-service 命令包测试全部通过，包含真实 PostgreSQL 准入锁、迁移和 RLS。未据此宣称线上 k3s 发布验收完成。
