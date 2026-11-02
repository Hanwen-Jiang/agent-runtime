"""ENV-01/Q99–105：只读权威状态，未知字段兼容，不暴露凭据。"""

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

from agentruntime.environment.v1 import environment_pb2 as env


def test_environment_service_is_read_only_and_tenant_scoped():
    service = env.DESCRIPTOR.services_by_name["EnvironmentService"]
    assert {m.name for m in service.methods} == {"GetEnvironment", "ListEnvironments"}
    for method in service.methods:
        assert method.input_type.fields_by_name["tenant_id"].number == 1
    fields = env.ListEnvironmentsRequest.DESCRIPTOR.fields_by_name
    assert {"workspace_id", "session_id", "limit"} <= fields.keys()


def test_record_keeps_authoritative_lifecycle_facts_without_credentials():
    fields = env.EnvRecord.DESCRIPTOR.fields_by_name
    assert {"env_ref", "tenant_id", "workspace_id", "session_id", "backend_env_id", "state",
            "created_at", "ready_at", "expires_at", "max_until", "health_confirmed_at", "reused_at", "ready", "reused"} <= fields.keys()
    assert not {"secret", "token", "credential", "env", "reason"} & fields.keys()
    for name in ("created_at", "health_confirmed_at", "reused_at", "expires_at"):
        assert fields[name].message_type.full_name == "google.protobuf.Timestamp"
    assert env.EnvironmentState.DESCRIPTOR.values_by_number[0].name == "ENVIRONMENT_STATE_UNSPECIFIED"


def test_old_reader_ignores_new_fields():
    file = descriptor_pb2.FileDescriptorProto(name="old_env.proto", package="old", syntax="proto3")
    message = file.message_type.add(name="EnvRecord")
    message.field.add(name="env_ref", number=1, type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING)
    pool = descriptor_pool.DescriptorPool()
    pool.Add(file)
    old = message_factory.GetMessageClass(pool.FindMessageTypeByName("old.EnvRecord"))()
    original = env.EnvRecord(env_ref="sbe-a", tenant_id="t1", ready=True, reused=True)
    old.ParseFromString(original.SerializeToString())
    assert old.env_ref == "sbe-a"
