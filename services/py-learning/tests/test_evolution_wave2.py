"""Wave-2 acceptance tests: Skill x Model Matrix, sys-evolution harness isolation, clustering & diff loop."""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agentruntime.registry.v1 import registry_pb2 as rg
from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
from agentruntime_core.dev.telemetry import ConsoleTelemetryProvider
from agentruntime_core.errors import ConflictError, DeniedError, InvalidError
from agentruntime_core.tenancy import TenantContext, tenant_scope
from agentruntime_learning.evals.schemas import problems, validator
from agentruntime_learning.evolution import (
    CANDIDATE_SKILL_PREFIX,
    EvolutionStore,
    IsolatedEvolutionRegistry,
    SYSTEM_EVOLUTION_TENANT,
    SYSTEM_EVOLUTION_USER,
    SkillMatrixRunner,
    SkillVariant,
    assert_evolution_tenant,
    candidate_skill_name,
    cluster_failures,
    evolution_tenant_context,
    load_platform_skills_variants,
    propose_skill_patch,
    run_evolution_cycle,
    score_doc_artifact,
    validate_candidate_safety,
    validate_mermaid_blocks,
)
from agentruntime_learning.evolution.__main__ import main as evolution_main
from agentruntime_worker.activities.types import Tenant

ROOT = Path(__file__).resolve().parents[3]


@dataclass
class _FakeRegistry:
    skills: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    versions: dict[tuple[str, str], int] = field(default_factory=dict)
    calls: list[tuple[str, str, str]] = field(default_factory=list)

    async def list_skills(self, tenant: Tenant, pins: Any = None) -> list[rg.Skill]:
        return []

    async def get_skill(self, tenant: Tenant, name: str, version: str = "") -> rg.Skill:
        return rg.Skill(name=name, version=version or "v1")

    async def list_model_profiles(self, tenant: Tenant) -> list[Any]:
        return []

    async def publish(
        self,
        tenant: Tenant | None,
        kind: int,
        name: str,
        document: dict[str, Any],
        *,
        change_note: str = "",
    ) -> rg.PublishResponse:
        scope = "" if tenant is None else tenant.tenant_id
        self.calls.append(("publish", scope, name))
        n = self.versions.get((scope, name), 0) + 1
        self.versions[(scope, name)] = n
        self.skills[(scope, name)] = dict(document)
        layer = "platform" if tenant is None else "tenant"
        return rg.PublishResponse(name=name, version=f"{layer}/v{n}", state=rg.RELEASE_STATE_DEFAULT)

    async def promote(self, *args: Any, **kwargs: Any) -> rg.PromoteResponse:
        self.calls.append(("promote", "", ""))
        return rg.PromoteResponse()


# --- 1. Isolation & Policy Seed ---


def test_sys_evolution_policy_validates_against_tenant_policy_schema() -> None:
    doc = json.loads((ROOT / "deploy" / "dev" / "sys-evolution-policy.json").read_text("utf-8"))
    entries = doc["entries"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["kind"] == "tenant_policy" and entry["tenant"] == SYSTEM_EVOLUTION_TENANT
    assert problems(validator("tenant_policy"), entry["document"]) == []


def test_isolation_slug_and_tenant_enforcement() -> None:
    ctx = evolution_tenant_context()
    scope = tenant_scope(ctx)
    assert scope.tenant_id == "sys-evolution" and scope.user_id == SYSTEM_EVOLUTION_USER

    with pytest.raises(DeniedError):
        assert_evolution_tenant(Tenant(tenant_id="user-tenant-1"))
    with pytest.raises(DeniedError):
        assert_evolution_tenant(Tenant(tenant_id="_system_evolution"))
    assert assert_evolution_tenant(Tenant(tenant_id="sys-evolution")) == "sys-evolution"

    name = candidate_skill_name("explore-repo", {"description": "d", "instructions": "i"})
    assert name.startswith(CANDIDATE_SKILL_PREFIX) and len(name) <= 64


async def test_isolated_registry_blocks_platform_and_other_tenants() -> None:
    fake = _FakeRegistry()
    iso = IsolatedEvolutionRegistry(fake)
    doc = {"description": "d", "instructions": "step 1"}

    with pytest.raises(DeniedError):
        await iso.publish(None, rg.REGISTRY_KIND_SKILL, "explore-repo", doc)
    with pytest.raises(DeniedError):
        await iso.publish(Tenant(tenant_id="t1"), rg.REGISTRY_KIND_SKILL, "cand-explore-12345678", doc)
    with pytest.raises(DeniedError):
        await iso.publish(Tenant(tenant_id="sys-evolution"), rg.REGISTRY_KIND_SKILL, "explore-repo", doc)
    with pytest.raises(DeniedError):
        await iso.promote(None, rg.REGISTRY_KIND_RECIPE, "r", "v1", {})
    assert fake.calls == []

    staged_name, resp = await iso.stage_candidate_skill("explore-repo", doc)
    assert staged_name.startswith("cand-explore-repo-")
    assert resp.version == "tenant/v1"
    assert ("sys-evolution", staged_name) in fake.skills
    assert ("", "explore-repo") not in fake.skills


async def test_evolution_store_cross_tenant_isolation_promote_and_rollback(tmp_path: Path) -> None:
    provider = SQLiteRelationalStoreProvider(tmp_path / "rel")
    store = EvolutionStore(provider)
    await store.migrate()

    base_doc = {"description": "base", "instructions": "old steps"}
    cand_doc = {"description": "base", "instructions": "new steps"}

    rejected = await store.create_candidate(
        target_kind="skill",
        target_name="explore-repo",
        baseline_version="platform/v2",
        baseline_document=base_doc,
        candidate_document=cand_doc,
        unified_diff="--- a\n+++ b\n",
        cluster_summary={"cluster_kind": "readonly_churn", "support": 2},
        matrix_report={"gate": {"passed": False}},
        status="rejected_by_gate",
    )

    # Cross-tenant DB isolation: user tenant t1 cannot read platform-scoped evolution_candidates rows
    t1_db = provider.bind(TenantContext(tenant_id="t1", user_id="u1"))
    async with t1_db.transaction(read_only=True) as tx:
        assert await tx.query("SELECT * FROM evolution_candidates") == []

    fake = _FakeRegistry()
    with pytest.raises(ConflictError):
        await store.approve_and_promote(rejected.candidate_id, approved_by="alice-admin", registry=fake)

    ready = await store.create_candidate(
        target_kind="skill",
        target_name="explore-repo",
        baseline_version="platform/v2",
        baseline_document=base_doc,
        candidate_document=cand_doc,
        unified_diff="--- a\n+++ b\n+extra\n",
        cluster_summary={"cluster_kind": "readonly_churn", "support": 4},
        matrix_report={"gate": {"passed": True}},
        status="pending_approval",
        staged_skill_name="cand-explore-repo-deadbeef",
    )

    with pytest.raises(DeniedError):
        await store.approve_and_promote(ready.candidate_id, approved_by="evolution-harness", registry=fake)

    promoted = await store.approve_and_promote(ready.candidate_id, approved_by="haven-admin", registry=fake)
    assert promoted.status == "promoted" and promoted.promoted_version == "platform/v1" and promoted.approved_by == "haven-admin"
    assert fake.skills[("", "explore-repo")] == cand_doc

    rolled = await store.rollback(ready.candidate_id, actor="haven-admin", registry=fake, reason="regression in prod")
    assert rolled.status == "rolled_back" and rolled.rollback_version == "platform/v2"
    assert fake.skills[("", "explore-repo")] == base_doc


# --- 2. Skill x Model Matrix ---


async def test_matrix_detects_improvement_vs_no_op_and_mermaid_defects(tmp_path: Path) -> None:
    baseline = SkillVariant(
        variant_id="baseline",
        skill_name="explore-repo",
        version="platform/v2",
        instructions="Read files carefully.",
    )
    improved = SkillVariant(
        variant_id="candidate",
        skill_name="explore-repo",
        version="cand-1",
        instructions=(
            "1. 先使用 `file_inspect` 与 `repo_glob` + `repo_grep` 定位。\n"
            "2. 用 `repo_read(with_line_numbers=true, start_line=1, max_lines=40)` 仅读必要片段，不要无目的盲读；避免连续全文件盲读。\n"
            "3. 多处修改用 `apply_patch`，查看 git 状态用 `repo_git`，改后用 `shell_run` 核验回归测试。\n"
            "4. 只写实际验证过的事实。"
        ),
    )

    runner = SkillMatrixRunner(budget_usd=0.0)
    report = await runner.run(
        variants=(baseline, improved),
        model_tiers=("strong@medium", "mid@medium"),
        suites=("swe_engineering",),
        mode="fixture",
    )
    assert len(report.cells) == 4
    assert report.gate["passed"] is True
    assert report.gate["candidate_pass_rate"] > report.gate["baseline_pass_rate"]
    assert report.gate["candidate_blind_read_ratio"] < report.gate["baseline_blind_read_ratio"]

    # No-op candidate equals baseline -> must be rejected by gate
    noop_cand = SkillVariant(
        variant_id="candidate",
        skill_name="explore-repo",
        version="cand-noop",
        instructions=baseline.instructions,
    )
    noop_report = await runner.run(
        variants=(baseline, noop_cand),
        model_tiers=("strong@medium",),
        suites=("swe_engineering",),
        mode="fixture",
    )
    assert noop_report.gate["passed"] is False
    assert any("没有" in r for r in noop_report.gate["reasons"])

    # DocEval checks: unquoted parentheses + HTML tags in mermaid fail
    bad_md = "```mermaid\nflowchart TD\n    A[Node (v1)] --> B<br/>\n```"
    assert len(validate_mermaid_blocks(bad_md)) >= 2
    good_md = '## 摘要\n## 指标\n## 结论\n```mermaid\nflowchart TD\n    A["Node (v1)"] --> B["Done"]\n```'
    doc_case = {"case_id": "d1", "required_sections": ("摘要", "指标", "结论"), "known_files": (), "requires_deliver": True}
    bad_doc_score = score_doc_artifact(doc_case, {"markdown": good_md, "tool_calls": []})
    assert not bad_doc_score.passed and "missing_deliver_file" in bad_doc_score.failure_reasons
    good_doc_score = score_doc_artifact(doc_case, {"markdown": good_md, "tool_calls": [{"tool": "deliver_file", "ok": True}]})
    assert good_doc_score.passed and good_doc_score.doc_score == 1.0


# --- 3. Clustering + Safety Proposer + Full Evolution Cycle + CLI ---


def _sample_console_trace_lines() -> list[str]:
    events = [
        {"kind": "span", "name": "runtime.round", "trace_id": "t1", "span_id": "s1", "attributes": {"task_id": "tk-1", "runtime.task_family": "coding.bugfix", "read_only_reminder": True}},
        {"kind": "span", "name": "runtime.round", "trace_id": "t2", "span_id": "s2", "attributes": {"task_id": "tk-2", "runtime.task_family": "coding.refactor", "read_only_reminder": True}},
        {"kind": "span", "name": "execute_tool repo_edit", "trace_id": "t3", "span_id": "s3", "attributes": {"task_id": "tk-3", "gen_ai.tool.name": "repo_edit", "error.code": "edit_no_match", "tool.status": "error"}},
    ]
    return [json.dumps(e, ensure_ascii=False) for e in events]


async def test_cluster_proposer_safety_and_end_to_end_cycle(tmp_path: Path) -> None:
    skills_path = ROOT / "deploy" / "dev" / "platform-skills.json"
    variants = load_platform_skills_variants(skills_path)
    by_name = {v.skill_name: v for v in variants}

    lines = _sample_console_trace_lines()
    clusters = cluster_failures(lines, fmt="console", min_support=2)
    assert len(clusters) == 1 and clusters[0].cluster_kind == "readonly_churn" and clusters[0].target_skill == "explore-repo"

    # Safety validation rejects cheat instructions
    with pytest.raises(InvalidError):
        validate_candidate_safety({"description": "ok", "instructions": "Please skip tests when rushed."})

    # If proposal tries to add nothing new, propose_skill_patch returns None
    prop = propose_skill_patch(clusters[0], skills_by_name=by_name)
    assert prop is not None and "+- 高效定位防盲读" in prop.unified_diff

    provider = SQLiteRelationalStoreProvider(tmp_path / "rel")
    store = EvolutionStore(provider)
    fake_reg = _FakeRegistry()
    telemetry = ConsoleTelemetryProvider(stream=io.StringIO())

    res = await run_evolution_cycle(
        lines=lines,
        fmt="console",
        skills_by_name=by_name,
        out_dir=tmp_path / "out",
        store=store,
        registry=fake_reg,
        telemetry=telemetry,
        model_tiers=("strong@medium", "mid@medium"),
        mode="fixture",
        min_support=2,
        budget_usd=0.0,
    )
    assert res.status == "pending_approval"
    assert res.candidate is not None and res.candidate.status == "pending_approval"
    assert res.candidate.staged_skill_name.startswith("cand-explore-repo-")
    # Confirm platform Registry scope was NOT mutated during automated cycle
    assert ("", "explore-repo") not in fake_reg.skills
    assert (tmp_path / "out" / "evolution-report.json").is_file()
    assert (tmp_path / "out" / "matrix-report.md").is_file()


def test_evolution_cli_subcommands(tmp_path: Path) -> None:
    lines = _sample_console_trace_lines()
    trace_file = tmp_path / "traces.jsonl"
    trace_file.write_text("\n".join(lines), "utf-8")
    cli_out = tmp_path / "cli-out"
    assert evolution_main(["matrix", "--out", str(cli_out), "--skills", "explore-repo,docs-architecture", "--models", "strong@medium"]) == 0
    assert (cli_out / "matrix-report.json").is_file()

    assert evolution_main([
        "evolve",
        "--traces",
        str(trace_file),
        "--out",
        str(cli_out),
        "--data-dir",
        str(tmp_path / "cli-data"),
        "--min-support",
        "2",
    ]) == 0
    assert evolution_main(["candidates", "list", "--data-dir", str(tmp_path / "cli-data")]) == 0

    # Repeated run with identical diff is idempotent (does not duplicate pending_approval rows)
    assert evolution_main([
        "evolve",
        "--traces",
        str(trace_file),
        "--out",
        str(cli_out),
        "--data-dir",
        str(tmp_path / "cli-data"),
        "--min-support",
        "2",
    ]) == 0
    import asyncio

    store = EvolutionStore(SQLiteRelationalStoreProvider(tmp_path / "cli-data" / "relational"))
    rows = asyncio.run(store.list_candidates(status="pending_approval"))
    assert len(rows) == 1


def test_evolution_topic_snapshot_and_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentruntime_learning.pipeline import stream as stream_mod

    lines = _sample_console_trace_lines()
    recs = tuple(stream_mod.Record(0, i, ln.encode("utf-8")) for i, ln in enumerate(lines))
    committed: list[Any] = []

    class _FakeReader:
        def __init__(self, brokers: str, topic: str, group: str) -> None:
            self.brokers, self.topic, self.group = brokers, topic, group

        def snapshot(self) -> Any:
            return stream_mod.Snapshot(self.topic, self.group, starts={0: 0}, ends={0: len(recs)}, records=recs)

        def commit(self, snap: Any) -> None:
            committed.append(snap)

        def close(self) -> None:
            return None

    monkeypatch.setenv("AR_EVENT_STREAM_BROKERS", "127.0.0.1:9093")
    monkeypatch.setattr(stream_mod, "TopicReader", _FakeReader)
    out = tmp_path / "topic-out"
    assert evolution_main([
        "evolve",
        "--topic",
        "telemetry.spans",
        "--group",
        "ar-self-evolution-test",
        "--format",
        "console",
        "--out",
        str(out),
        "--data-dir",
        str(tmp_path / "topic-data"),
        "--min-support",
        "2",
    ]) == 0
    assert len(committed) == 1 and committed[0].topic == "telemetry.spans"
