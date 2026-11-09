"""S-01 契约骨架的结构检查：按生成代码的描述符逐项核对计划 S-01 的交付第 2 条与“难点与防错”。

只检查跨服务、跨语言必须对齐的骨架；消息里的其余字段由人审。
"""

from __future__ import annotations

import pytest
from google.protobuf.descriptor import Descriptor, FieldDescriptor

from contract_tests import protos

COMMON = "agentruntime.common.v1"
TASK_API = "agentruntime.taskapi.v1"
WORKFLOW = "agentruntime.workflow.v1"
REGISTRY = "agentruntime.registry.v1"
MODEL_GATEWAY = "agentruntime.modelgateway.v1"
MEMORY = "agentruntime.memory.v1"
EXPECTED_PACKAGES = {COMMON, TASK_API, WORKFLOW, REGISTRY, MODEL_GATEWAY, MEMORY}

TENANT_CONTEXT = f"{COMMON}.TenantContext"
JSON_PAYLOAD = f"{COMMON}.JsonPayload"
USAGE = f"{COMMON}.Usage"
STRUCT = "google.protobuf.Struct"
VALUE = "google.protobuf.Value"
JSON_CARRIERS = {STRUCT, VALUE, JSON_PAYLOAD}

TASK_SERVICE = f"{TASK_API}.TaskService"
TASK_SERVICE_METHODS = {"SubmitTask", "CancelTask", "SignalTask", "QueryTask"}

# 内部服务：先定方法签名（计划 S-01 交付第 2 条）。方法集合依据设计书 3.1.2、4.8、3.4.1。
INTERNAL_SERVICES = {
    f"{REGISTRY}.RegistryService": {
        "Publish",  # 发布新版本（先影子运行）
        "Promote",  # 评测达标后设为默认
        "ResolvePinnedVersions",  # Task 启动时固定版本
        "GetNodeTemplate",
        "ListRecipes",  # 按租户与任务族查询
        "GetModelProfile",
        "GetTenantPolicy",
    },
    f"{MODEL_GATEWAY}.ModelGatewayService": {"GetCapabilities", "Generate", "StreamGenerate", "Score", "Embed", "Classify"},
    f"{MEMORY}.MemoryService": {"Search", "SubmitCandidates", "RecordFeedback", "Delete"},
}

WORKFLOW_MESSAGES = {
    "SessionWorkflowInput",
    "SessionState",
    "SubmitTaskSignal",
    "CancelTaskSignal",
    "TaskWorkflowInput",
    "TaskWorkflowResult",
    "TaskStateQueryResult",
    "HumanReplySignal",
    "PinnedVersions",
}

# Node 的 inputs、result、模板参数与 /schemas 中的对象都是 JSON Schema 载荷（设计书 4.1），
# 在 proto 里只能用 Struct、Value 或 JsonPayload 携带。
PAYLOAD_FIELD_NAMES = {
    "inputs",
    "result",
    "params",
    "template_params",
    "defaults",
    "task_state",
    "task_plan",
    "stage",
    "node_output",
    "hook_result",
    "memory_item",
    "memory_items",
}
SCHEMA_OBJECT_NAMES = {
    "AtomicNode",
    "GoalNode",
    "Stage",
    "TaskState",
    "TaskPlan",
    "NodeOutput",
    "Verdict",
    "HookResult",
    "MemoryItem",
}

INT64_TYPES = {
    FieldDescriptor.TYPE_INT64,
    FieldDescriptor.TYPE_UINT64,
    FieldDescriptor.TYPE_SINT64,
    FieldDescriptor.TYPE_FIXED64,
    FieldDescriptor.TYPE_SFIXED64,
}


def _field(message_name: str, field_name: str) -> FieldDescriptor:
    desc = protos.message(message_name)
    field = desc.fields_by_name.get(field_name)
    assert field is not None, f"{message_name} 缺少字段 {field_name}"
    return field


def _reachable_message_types(desc: Descriptor, seen: set[str] | None = None) -> set[str]:
    seen = set() if seen is None else seen
    for field in desc.fields:
        if field.type != FieldDescriptor.TYPE_MESSAGE:
            continue
        name = field.message_type.full_name
        if name in seen:
            continue
        seen.add(name)
        if not name.startswith("google.protobuf."):
            _reachable_message_types(field.message_type, seen)
    return seen


# --- 包与通用约定 ---


def test_expected_packages_exist():
    packages = {f.package for f in protos.files()}
    missing = EXPECTED_PACKAGES - packages
    assert not missing, f"缺少 proto 包：{sorted(missing)}"


def test_enum_zero_value_is_unspecified():
    bad = []
    for desc in protos.enums():
        expected = f"{protos.upper_snake(desc.name)}_UNSPECIFIED"
        zero = desc.values_by_number.get(0)
        if zero is None or zero.name != expected:
            bad.append(f"{desc.full_name}：0 值应为 {expected}")
    assert not bad, "\n".join(bad)


def test_enum_values_carry_enum_name_prefix():
    bad = []
    for desc in protos.enums():
        prefix = protos.upper_snake(desc.name) + "_"
        bad += [f"{desc.full_name}.{v.name}" for v in desc.values if not v.name.startswith(prefix)]
    assert not bad, f"枚举值缺少前缀：{bad}"


def test_id_fields_are_strings():
    """ID 一律是带前缀的字符串；只有 SSE 的递增 event_id 是整数。"""
    bad = []
    for desc in protos.messages():
        for field in desc.fields:
            if field.name.endswith("_id") and not field.name.endswith("event_id"):
                if field.type != FieldDescriptor.TYPE_STRING:
                    bad.append(f"{desc.full_name}.{field.name}")
    assert not bad, f"ID 字段不是 string：{bad}"


# --- common ---


def test_tenant_context_fields():
    for name in ("tenant_id", "user_id", "workspace_id", "session_id"):
        assert _field(TENANT_CONTEXT, name).type == FieldDescriptor.TYPE_STRING, name
    trace = _field(TENANT_CONTEXT, "trace")
    assert trace.type == FieldDescriptor.TYPE_MESSAGE, "trace 上下文用一个消息承载 W3C traceparent 与 tracestate"
    for name in ("traceparent", "tracestate"):
        assert name in trace.message_type.fields_by_name, name


def test_error_model():
    categories = protos.enum_values(protos.enum(f"{COMMON}.ErrorCategory"))
    assert sorted(categories) == sorted(["transient", "invalid", "denied", "not_found", "conflict"])
    error = protos.message(f"{COMMON}.Error")
    category = error.fields_by_name.get("category")
    assert category is not None and category.enum_type.full_name == f"{COMMON}.ErrorCategory"
    assert error.fields_by_name["message"].type == FieldDescriptor.TYPE_STRING
    # 是否可重试只由 category 决定：transient 可重试，其余不可重试（计划 S-02）。
    assert "retryable" not in error.fields_by_name


def test_usage_metering_uses_integers():
    """计量：token 与成本。金额用整数微美元，避免浮点误差。"""
    for name in ("input_tokens", "output_tokens", "cost_usd_micros"):
        assert _field(USAGE, name).type in INT64_TYPES, name


def test_json_payload_carries_schema_reference():
    desc = protos.message(JSON_PAYLOAD)
    schema_id = desc.fields_by_name.get("schema_id")
    assert schema_id is not None and schema_id.type == FieldDescriptor.TYPE_STRING
    bodies = [
        f
        for f in desc.fields
        if f.name != "schema_id"
        and (f.type == FieldDescriptor.TYPE_BYTES or protos.message_type_name(f) in {STRUCT, VALUE})
    ]
    assert len(bodies) == 1, "JsonPayload 只有一个载荷本体：bytes、Struct 或 Value"


# --- Task API ---


def test_task_service_methods():
    methods = set(protos.service(TASK_SERVICE).methods_by_name)
    missing = TASK_SERVICE_METHODS - methods
    assert not missing, f"Task API 缺少方法：{sorted(missing)}"


def test_submit_task_is_deduplicated_and_returns_subscription():
    """重复提交返回同一个 task_id（计划 S-03）；提交后立即返回 task_id 与 SSE 订阅地址（设计书 2.5）。"""
    assert _field(f"{TASK_API}.SubmitTaskRequest", "client_request_id").type == FieldDescriptor.TYPE_STRING
    for name in ("task_id", "events_url"):
        assert _field(f"{TASK_API}.SubmitTaskResponse", name).type == FieldDescriptor.TYPE_STRING, name


def test_task_event_envelope():
    """SSE 事件信封：递增 event_id、type、payload。"""
    event = f"{TASK_API}.TaskEvent"
    assert _field(event, "event_id").type in INT64_TYPES
    assert _field(event, "type").type in (FieldDescriptor.TYPE_ENUM, FieldDescriptor.TYPE_STRING)
    assert protos.message_type_name(_field(event, "payload")) in JSON_CARRIERS


def test_external_requests_do_not_carry_tenant_context():
    """租户由服务端从认证身份推出；外部请求里只允许一个用于一致性校验的 tenant_id 字符串。"""
    bad = [
        method.full_name
        for method in protos.service(TASK_SERVICE).methods
        if TENANT_CONTEXT in _reachable_message_types(method.input_type)
    ]
    assert not bad, f"外部请求携带了 TenantContext：{bad}"


# --- 内部服务 ---


@pytest.mark.parametrize("service_name", sorted(INTERNAL_SERVICES))
def test_internal_service_methods(service_name):
    methods = set(protos.service(service_name).methods_by_name)
    missing = INTERNAL_SERVICES[service_name] - methods
    assert not missing, f"{service_name} 缺少方法：{sorted(missing)}"


@pytest.mark.parametrize("service_name", sorted(INTERNAL_SERVICES))
def test_internal_requests_carry_tenant_context_first(service_name):
    """内部调用都带租户上下文（设计书 4.1），统一放在字段 1、名为 tenant。"""
    bad = []
    for method in protos.service(service_name).methods:
        first = method.input_type.fields_by_number.get(1)
        if first is None or first.name != "tenant" or protos.message_type_name(first) != TENANT_CONTEXT:
            bad.append(method.full_name)
    assert not bad, f"请求的字段 1 不是 TenantContext tenant：{bad}"


def test_model_gateway_returns_usage():
    """ModelProvider 模型调用回传计量；能力协商不调用模型，也不产生 Usage。"""
    bad = []
    for method in protos.service(f"{MODEL_GATEWAY}.ModelGatewayService").methods:
        if method.name == "GetCapabilities":
            continue
        usage = method.output_type.fields_by_name.get("usage")
        if usage is None or protos.message_type_name(usage) != USAGE:
            bad.append(method.full_name)
    assert not bad, f"响应缺少 Usage：{bad}"


def test_stream_generate_is_server_streaming():
    method = protos.service(f"{MODEL_GATEWAY}.ModelGatewayService").methods_by_name["StreamGenerate"]
    assert protos.server_streaming(method)


# --- workflow ---


def test_workflow_messages_exist():
    present = {d.name for d in protos.messages() if d.full_name.startswith(WORKFLOW + ".")}
    missing = WORKFLOW_MESSAGES - present
    assert not missing, f"workflow 包缺少：{sorted(missing)}"


def test_task_workflow_input_carries_tenant_and_pinned_versions():
    message = f"{WORKFLOW}.TaskWorkflowInput"
    assert protos.message_type_name(_field(message, "tenant")) == TENANT_CONTEXT
    assert _field(message, "task_id").type == FieldDescriptor.TYPE_STRING
    assert protos.message_type_name(_field(message, "pinned_versions")) == f"{WORKFLOW}.PinnedVersions"


def test_pinned_versions_manifest():
    """固定的版本清单：Recipe、Node Template、Model Profile 的版本（设计书 3.1.2、附录 K.1）。"""
    message = f"{WORKFLOW}.PinnedVersions"
    assert _field(message, "recipe").type == FieldDescriptor.TYPE_STRING
    for name in ("node_templates", "model_profiles"):
        field = _field(message, name)
        assert protos.is_map(field), f"{name} 应为 map<string, string>"
        entry = field.message_type.fields_by_name
        assert entry["key"].type == FieldDescriptor.TYPE_STRING
        assert entry["value"].type == FieldDescriptor.TYPE_STRING


def test_session_workflow_input_can_carry_drained_signals():
    """Continue-As-New 前把未处理的提交信号排空，带入新的运行（计划 S-03）。"""
    message = f"{WORKFLOW}.SessionWorkflowInput"
    assert protos.message_type_name(_field(message, "tenant")) == TENANT_CONTEXT
    carriers = [
        f
        for f in protos.message(message).fields
        if protos.is_repeated(f) and protos.message_type_name(f) == f"{WORKFLOW}.SubmitTaskSignal"
    ]
    assert carriers, "SessionWorkflowInput 需要一个 repeated SubmitTaskSignal 字段承接续接前未处理的信号"


def test_session_state_does_not_hold_task_state():
    """Task State 不放在 Session 工作流里（计划 S-03）。"""
    for root in (f"{WORKFLOW}.SessionState", f"{WORKFLOW}.SessionWorkflowInput"):
        names = {root} | _reachable_message_types(protos.message(root))
        holders = [
            f"{name}.task_state"
            for name in sorted(names)
            if not name.startswith("google.protobuf.")
            and "task_state" in protos.message(name).fields_by_name
        ]
        assert not holders, f"Session 工作流的状态里出现了 Task State：{holders}"


@pytest.mark.parametrize("enum_name", ["WorkflowType", "SignalName", "QueryName", "TaskQueue"])
def test_cross_language_names_come_from_wire_name(enum_name):
    """工作流类型、信号、查询、任务队列名在 proto 里定义一次，两种语言都从 wire_name 读（附录 K.5）。"""
    names = protos.option_values(protos.enum(f"{WORKFLOW}.{enum_name}"), protos.WIRE_NAME)
    assert names and all(names.values()), f"{enum_name} 有值缺少 wire_name：{names}"
    assert len(set(names.values())) == len(names), f"{enum_name} 的 wire_name 重复：{names}"


def test_task_queues_cover_appendix_k5():
    queues = set(protos.option_values(protos.enum(f"{WORKFLOW}.TaskQueue"), protos.WIRE_NAME).values())
    assert {"session", "task", "decision", "model", "program", "offline"} <= queues


def test_task_state_query_returns_payload():
    field = _field(f"{WORKFLOW}.TaskStateQueryResult", "task_state")
    assert protos.message_type_name(field) == JSON_PAYLOAD


# --- 骨架与载荷分层 ---


def test_payload_fields_use_json_carriers():
    bad = []
    for desc in protos.messages():
        for field in desc.fields:
            if field.name not in PAYLOAD_FIELD_NAMES:
                continue
            if field.full_name == "agentruntime.personalconfig.v1.PersonalConfigDocument.defaults":
                # D-27 defaults are three registered-resource references, not
                # arbitrary Node/Template parameter defaults. Keep their wire
                # shape strict while retaining the payload rule everywhere else.
                assert field.message_type.full_name == "agentruntime.personalconfig.v1.PersonalConfigDefaults"
                refs = field.message_type.fields_by_name
                assert set(refs) == {"skill_names", "mcp_connection_ids", "environment_id"}
                assert all(ref.type == ref.TYPE_STRING for ref in refs.values())
                assert refs["skill_names"].is_repeated and refs["mcp_connection_ids"].is_repeated
                assert not refs["environment_id"].is_repeated
                continue
            carried = field
            if protos.is_map(field):
                carried = field.message_type.fields_by_name["value"]
            if protos.message_type_name(carried) not in JSON_CARRIERS:
                bad.append(f"{desc.full_name}.{field.name}")
    assert not bad, f"这些字段应是 JSON Schema 载荷（Struct、Value 或 JsonPayload）：{bad}"


def test_schema_objects_are_not_proto_messages():
    names = {d.name for d in protos.messages()}
    clash = sorted(names & SCHEMA_OBJECT_NAMES)
    assert not clash, f"这些对象由 /schemas 定义，不写成 proto message：{clash}"
