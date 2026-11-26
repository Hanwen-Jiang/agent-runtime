"""Verify structured OTel spans emitted by py-worker feed Wave 2 evolution clustering."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agentruntime_core.providers.tool_driver import Effect, ToolSpec
from agentruntime_core.tenancy import TenantContext

from agentruntime_worker.activities.types import ReviewRoundInput, Tenant
from agentruntime_worker.reviewer.activities import ReviewerActivities, StaticCatalogSource
from agentruntime_worker.reviewer.reviewer import Reviewer
from agentruntime_worker.reviewer.rules import ReviewCatalog
from .executor_harness import make_fixture

_LEARNING_SRC = Path(__file__).resolve().parents[3] / "py-learning" / "src"
if str(_LEARNING_SRC) not in sys.path:
    sys.path.insert(0, str(_LEARNING_SRC))

from agentruntime_learning.evolution.clustering import cluster_failures  # noqa: E402


class _FakeSchemas:
    def get(self, schema_id: str) -> Any:
        return None


class _FakeModels:
    async def generate(self, req: Any) -> Any:  # pragma: no cover
        raise AssertionError("not used")

    async def stream_generate(self, req: Any) -> Any:  # pragma: no cover
        raise AssertionError("not used")


def _export_to_console_lines(spans: list[Any]) -> list[str]:
    lines: list[str] = []
    for s in spans:
        attrs = dict(s.attributes or {})
        rec = {
            "kind": "span",
            "name": s.name,
            "trace_id": format(s.context.trace_id, "032x"),
            "span_id": format(s.context.span_id, "016x"),
            "attributes": attrs,
        }
        lines.append(json.dumps(rec, ensure_ascii=False))
    return lines


@pytest.mark.asyncio
async def test_worker_tool_and_reviewer_spans_cluster_into_evolution_targets(tmp_path: Path) -> None:
    exporter = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = tp.get_tracer("test-worker")

    fx = make_fixture(tmp_path)

    async def fail_edit(_inv: Any) -> dict[str, Any]:
        return {"ok": False, "error": "old text no match in file"}

    fx.register("repo_edit", Effect.WRITE_REVERSIBLE)
    fx.behaviors["repo_edit"] = fail_edit
    executor = fx.executor

    for task_id in ("t_edit_1", "t_edit_2"):
        with tracer.start_as_current_span("activity.execute_node") as root_span:
            out = await executor._tool(
                {"id": "edit", "template": "repo_edit@v1", "effect": "write_reversible", "skill": "coding-engineering", "idempotency_key": f"idem-{task_id}", "inputs": {}},
                TenantContext(tenant_id="t1", workspace_id="w1"),
                "repo_edit",
                "v1",
                root_span,
                task_id=task_id,
            )
            assert out["ok"] is False
            root_span.set_attributes({"runtime.task_id": task_id, "runtime.task_family": "swe.bugfix", "skill.name": "coding-engineering"})

    reviewer_acts = ReviewerActivities(Reviewer(_FakeModels()), StaticCatalogSource(ReviewCatalog()), wrap_up=None)
    for task_id in ("t_rev_1", "t_rev_2"):
        with tracer.start_as_current_span("activity.review_round"):
            await reviewer_acts.review(
                ReviewRoundInput(
                    tenant=Tenant(tenant_id="t1", workspace_id="w1"),
                    task_id=task_id,
                    round_seq=1,
                    done_when="tests pass",
                    task_state={"goal": "fix bug", "personal_config": {"skills": [{"name": "bugfix-tdd"}]}},
                    stage={"round_seq": 1, "goal": "fix bug", "nodes": [{"id": "d1", "role": "deliver"}]},
                    stage_results={"d1": {"status": "succeeded", "result": {"summary": "fixed"}}},
                    history=[],
                    rules_only=True,
                    verification={"write_capable": True, "wrote": True, "ran_after_write": False, "docs_only": False},
                )
            )

    finished = list(exporter.get_finished_spans())
    # Verify structured OTel attributes required for Wave 2 queries & clustering
    edit_spans = [s for s in finished if (s.attributes or {}).get("tool.error_code") == "edit_no_match"]
    assert len(edit_spans) == 2
    assert edit_spans[0].attributes["failure.reason"] == "edit_no_match"
    assert edit_spans[0].attributes["skill.name"] == "coding-engineering"

    rev_spans = [s for s in finished if "rule_done_unverified" in str((s.attributes or {}).get("runtime.floor_reasons", ""))]
    assert len(rev_spans) == 2
    assert rev_spans[0].attributes["skill.name"] == "bugfix-tdd"
    assert "rule_done_unverified" in str(rev_spans[0].attributes["failure.reason"])

    clusters = cluster_failures(_export_to_console_lines(finished), fmt="console", min_support=2)
    by_kind = {c.cluster_kind: c for c in clusters}
    assert "edit_or_patch_conflict" in by_kind
    assert by_kind["edit_or_patch_conflict"].target_skill == "coding-engineering"
    assert "unverified_completion" in by_kind
    assert by_kind["unverified_completion"].target_skill == "bugfix-tdd"
