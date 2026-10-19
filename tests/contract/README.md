# S-01 契约验收测试

对照[强档实施计划](../../docs/plan/strong-tier-plan.md) S-01 的“验收”与“难点与防错”。

| 计划条目 | 测试 |
| --- | --- |
| 验收：`buf lint` 与 `buf breaking` 通过 | `test_toolchain.py`：`test_buf_lint`、`test_buf_format_is_clean`、`test_buf_breaking_against_baseline` |
| 验收：Go 与 Python 生成代码能编译或导入 | `test_toolchain.py`：`test_go_modules_build_vet_and_test`、`test_python_generated_code_imports`、`test_generated_code_is_up_to_date` |
| 验收：正例全部通过 schema 校验，反例全部失败 | `test_schema_examples.py`；样例在 `schemas/examples/` |
| 验收：跨语言往返，Go 编码 Python 解码、反向亦然 | `test_crosslang_roundtrip.py`，Go 侧工具 `go/cmd/payloadconv` |
| 验收：人审确认字段命名与附录 A 一致 | 自动部分：`test_enum_parity.py` 核对附录 A 的枚举取值，`test_terminology.py` 拦旧叫法。其余命名由人审，待审清单见 `schemas/examples/README.md` |
| 难点：跨语言载荷统一用 protobuf JSON 编码 | `test_crosslang_roundtrip.py` 断言 `encoding = json/protobuf`；fixture 必须覆盖超过 2^53 的 int64、含列表与 null 的 Struct、枚举、map、Timestamp、非 ASCII 字符串 |
| 难点：protobuf 只放骨架 | `test_proto_conventions.py`：`test_payload_fields_use_json_carriers`、`test_schema_objects_are_not_proto_messages`、`test_json_payload_carries_schema_reference` |
| 难点：租户上下文不能伪造 | `test_proto_conventions.py`：`test_external_requests_do_not_carry_tenant_context`、`test_internal_requests_carry_tenant_context_first`、`test_task_workflow_input_carries_tenant_and_pinned_versions` |
| 难点：枚举用附录 A 的名字、0 值为 `*_UNSPECIFIED`、只在一处定义 | `test_enum_parity.py`；`test_proto_conventions.py`：`test_enum_zero_value_is_unspecified`、`test_enum_values_carry_enum_name_prefix` |
| 难点：字段编号预留，破坏性变更由 `buf breaking` 拦截 | `test_toolchain.py`：`test_buf_breaking_detects_deleted_field`、`test_buf_breaking_detects_renumbered_field`（故意改坏一份副本，证明检查生效） |
| 交付：Buf、uv workspace、`go.work`、目录按设计书 4.15 | `test_toolchain.py`：`test_repository_layout`、`test_uv_workspace_members`、`test_uv_lock_is_current`、`test_go_workspace_lists_all_modules` |
| 交付：`common`、`task_api`、`workflow`、`registry`、`model_gateway`、`memory` | `test_proto_conventions.py` |
| 跨语言共用的名字只定义一次（实现时补充） | `test_proto_conventions.py`：`test_cross_language_names_come_from_wire_name`、`test_task_queues_cover_appendix_k5`；`test_enum_parity.py`：`test_id_prefixes_match_schema_patterns` |

## 不在本套测试里的

- 租户校验的运行时行为（请求里的 tenant_id 与认证身份不一致时拒绝）：S-01 只定契约，行为由 S-03 的 Task Service 与 S-12a 测试。
- Node id 唯一、`after` 引用存在、DAG 无环、template 在 Node Catalog 中存在：JSON Schema 表达不了，由 S-04、S-09 的代码校验。
- 幂等键的生成规则（不含 attempt 编号）：S-05。
- 消息里其余字段的命名与取舍：人审。

## 运行

依赖 buf、go、uv，环境说明见 CLAUDE.md 的“常用命令”。

```powershell
uv run --package agentruntime-contract-tests pytest tests/contract
```

缺少工具时测试直接失败。本地临时缺工具可设 `AR_ALLOW_MISSING_TOOLS=1` 改为跳过；CI 不设。
