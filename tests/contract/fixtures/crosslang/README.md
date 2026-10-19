# 跨语言往返 fixture

`test_crosslang_roundtrip.py` 用这里的每个 `*.json` 做三件事：Python 自身往返、Go 编码后 Python 解码、Python 编码后 Go 解码。两边都用 Temporal SDK 的 DataConverter，编码必须是 `json/protobuf`。

文件格式：

```json
{
  "type": "agentruntime.workflow.v1.TaskWorkflowInput",
  "message": { "...": "按 protojson 写的消息内容" }
}
```

测试要求：

- 至少覆盖 `test_crosslang_roundtrip.py` 中 `REQUIRED_TYPES` 列出的消息。
- TaskWorkflowInput 的 fixture 覆盖 `REQUIRED_COVERAGE`：map、repeated、枚举、Timestamp、含列表与 null 的 Struct、超过 2^53 的 int64、非 ASCII 字符串。
