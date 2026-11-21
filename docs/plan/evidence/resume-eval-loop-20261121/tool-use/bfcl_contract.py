"""Isolated BFCL pilot: only the candidate's extra system instruction differs."""

from agentruntime_learning.capability.base import Item
from agentruntime_learning.capability.evals.bfcl import BfclEval


INSTRUCTION = (
    "When constructing tool calls, omit optional arguments whose intended value "
    "equals their documented default. Include optional arguments when the user "
    "requests a value different from that default."
)


class DefaultAwareBfcl(BfclEval):
    async def run_item(self, item, gateway, *, candidate):
        payload = dict(item.payload)
        turns = payload["question"]
        if len(turns) != 1:
            raise ValueError("This pilot evaluates single-turn BFCL only")
        payload["question"] = [[{"role": "system", "content": INSTRUCTION}, *turns[0]]]
        return await super().run_item(Item(item.id, payload), gateway, candidate=candidate)


def make(**options):
    return DefaultAwareBfcl(**options)
