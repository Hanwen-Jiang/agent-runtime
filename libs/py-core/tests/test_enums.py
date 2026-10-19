"""Python 里的附录 A 枚举与 proto 一致。"""

from agentruntime.common.v1 import enums_pb2

from agentruntime_core.enums import Effect


def test_effect_mirrors_proto():
    proto = {
        name.removeprefix("EFFECT_").lower()
        for name, number in enums_pb2.Effect.items()
        if number != 0
    }
    assert {e.value for e in Effect} == proto


def test_write_effects():
    assert {e for e in Effect if e.is_write} == {Effect.WRITE_REVERSIBLE, Effect.WRITE_IRREVERSIBLE}
