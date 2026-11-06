#!/usr/bin/env python3
"""Prepare auditable capability-only Registry shadows, never fake an A/B gate.

The dedicated capability promotion contract is owned by the integration lane.
This tool prepares its proposed evidence but does not send Promote requests.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MODEL_SCHEMA = "https://agentruntime.example/schemas/model_profile.schema.json"
SERVICE = "/agentruntime.registry.v1.RegistryService/"
VERSION = "mx-capability-publication-bundle-v1"


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    from agentruntime_learning.capability import matrix, publication, publish
    from agentruntime_learning.capability.evals import bfcl

    if args.out.exists() and any(args.out.iterdir()):
        raise ValueError("use a new empty bundle directory; original evidence is immutable")
    report = json.loads(args.scores.read_text())
    rows = matrix.read_results(args.run)
    current = matrix.score_results(matrix.load_plan(args.run / "plan.json"), rows)
    if current["cells"] != report["cells"]:
        raise ValueError("saved score report differs from current strict rescoring")
    quality_cell = next(c for c in report["cells"] if c["role"] == "act" and c["evals"] == [{"name": "bfcl-v4", "version": "gorilla@6ea5797"}])
    quality_patch = publish.patch_from_scores({**report, "cells": [quality_cell]})
    raw = [r for r in rows if r["eval"] == "bfcl-v4" and r["role"] == "act"]
    source_hashes = {"raw_results": file_digest(args.run / "results.jsonl"), "original_plan": file_digest(args.run / "plan.json"),
                     "score_report": file_digest(args.scores), "registry_export": file_digest(args.registry_export),
                     "ledger_observation": file_digest(args.ledger)}
    dataset_files = {}
    for category in bfcl.DEFAULT_CATEGORIES:
        stem, question_sha, answer_sha = bfcl.FILES[category]
        for rel, expected in ((f"BFCL_v4_{stem}.json", question_sha), (f"possible_answer/BFCL_v4_{stem}.json", answer_sha)):
            observed = file_digest(args.dataset / rel)
            if observed != expected:
                raise ValueError("BFCL dataset checksum does not match its frozen source")
            dataset_files[rel] = observed
    data = bfcl.load(bfcl.DEFAULT_CATEGORIES, args.dataset)
    for row in raw:
        if row.get("contaminated") or row.get("error"):
            continue
        item = data[row["item"]]
        check = bfcl.ast_check(item["category"], item["function"], row["record"]["output"], item["ground_truth"])
        if float(check["valid"]) != row["score"]:
            raise ValueError("historical output no longer reproduces its strict BFCL score")
    rubric_files = {str(Path(module.__file__).relative_to(Path(__file__).resolve().parents[1])): file_digest(Path(module.__file__))
                    for module in (bfcl, matrix, publication)}
    ledger = json.loads(args.ledger.read_text())
    by_profile: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in ledger["rows"]:
        if row["node_id"] != "bfcl" or not row["task_id"].startswith("bfcl-v4:"):
            raise ValueError("unexpected ledger scope")
        by_profile[row["model_profile_id"]].append(row)
    audited_rows = copy.deepcopy(rows)
    for row in audited_rows:
        if row["eval"] != "bfcl-v4" or row["role"] != "act":
            continue
        attempts = [r for r in by_profile[row["candidate"]] if r["task_id"] == "bfcl-v4:" + row["item"]]
        row["cost_known"] = bool(attempts) and all(r["actual_usd_micros"] is not None for r in attempts)
        row["cost_ledger_audit"] = {
            "source_sha256": source_hashes["ledger_observation"],
            "reservation_ids": [r["reservation_id"] for r in attempts],
            "known_usd_micros": sum(r["actual_usd_micros"] or 0 for r in attempts),
            "pending_reserved_usd_micros": sum(r["reserved_usd_micros"] for r in attempts if r["actual_usd_micros"] is None),
        }
    audited_report = matrix.score_results(matrix.load_plan(args.run / "plan.json"), audited_rows)
    cell = next(c for c in audited_report["cells"] if c["role"] == "act")
    for original, audited in zip(quality_cell["candidates"], cell["candidates"], strict=True):
        if any(original[k] != audited[k] for k in ("candidate", "items", "p", "score", "ci", "contaminated")):
            raise ValueError("cost annotation must not change quality scoring")
    patch = publish.patch_from_scores({**audited_report, "cells": [cell]})
    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / "audited-results.jsonl").open("x", encoding="utf-8") as stream:
        for row in audited_rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    save(args.out / "audited-scores.json", audited_report)
    save(args.out / "excluded-candidates.json", [c for c in cell["candidates"] if c["candidate"] not in patch])
    source_hashes["audited_results"] = file_digest(args.out / "audited-results.jsonl")
    source_hashes["audited_score_report"] = file_digest(args.out / "audited-scores.json")
    export = json.loads(args.registry_export.read_text())
    profiles = {p["id"]: p for p in export["profiles"]}
    bundle = {"kind": VERSION, "prepared_at": datetime.now(timezone.utc).isoformat(), "inputs": source_hashes,
              "source_paths": {k: str(v.resolve()) for k, v in {"raw_results": args.run / "results.jsonl", "original_plan": args.run / "plan.json",
                "score_report": args.scores, "registry_export": args.registry_export, "ledger_observation": args.ledger}.items()},
              "dataset": {"version": "gorilla@" + bfcl.COMMIT, "files": dataset_files, "sha256": digest(dataset_files)},
              "rubric": {"version": publication.POLICY_VERSION, "files": rubric_files, "sha256": digest(rubric_files)},
              "candidate_count": len(cell["candidates"]), "quality_eligible_candidates": len(quality_patch),
              "publication_eligible_candidates": len(patch),
              "required_candidates": cell["publication"]["required_candidates"], "items": [],
              "promotion_contract_status": "dedicated capability-only evidence; never generic A/B",
              "promotion_schema": {"path": str(args.promotion_schema), "sha256": file_digest(args.promotion_schema)}}
    for candidate in cell["candidates"]:
        name = candidate["candidate"]
        if name not in patch:
            continue
        own = [r for r in raw if r["candidate"] == name]
        ids = {"bfcl-v4:" + r["item"] for r in own}
        attempts = by_profile[name]
        if any(r["task_id"] not in ids for r in attempts) or not ids <= {r["task_id"] for r in attempts}:
            raise ValueError("ledger does not cover the exact candidate sample set")
        known_micros = sum(r["actual_usd_micros"] or 0 for r in attempts)
        if abs(known_micros / 1e6 - candidate["cost_usd"]) > 0.0000001:
            raise ValueError("settled ledger and historical known cost differ")
        pending = [r for r in attempts if r["actual_usd_micros"] is None]
        baseline = profiles[name]
        identity = export["registry_revisions"][name]
        if identity["scope"] != "platform" or not re.fullmatch(r"v[1-9][0-9]*", identity["version"]):
            raise ValueError("export is not a real published Registry identity")
        updated = copy.deepcopy(baseline)
        dims = {(p["task_family"], p["role"], p["tier"]) for p in patch[name]}
        kept = [p for p in updated.get("capability_scores", []) if (p["task_family"], p["role"], p["tier"]) not in dims]
        updated["capability_scores"] = sorted([*kept, *patch[name]], key=lambda p: (p["task_family"], p["role"], p["tier"]))
        problems = publish.profile_problems(updated)
        if problems:
            raise ValueError("candidate profile violates schema: " + "; ".join(problems))
        slug = hashlib.sha256(name.encode()).hexdigest()[:20]
        folder = args.out / "candidates" / slug
        cost = {"known": not pending, "known_usd": known_micros / 1e6, "actual_usd": known_micros / 1e6 if not pending else None,
                "pending_attempts": len(pending), "pending_reserved_usd": sum(r["reserved_usd_micros"] for r in pending) / 1e6,
                "scope": "evals model_usage BFCL attempts; no fabricated cost_per_success or budget pass"}
        evidence = {"evidence_kind": "capability_scores", "publication_policy": publication.POLICY_VERSION,
                    "profile_id": name, "task_family": cell["task_family"], "role": cell["role"], "tier": cell["tier"],
                    "samples": candidate["items"], "repeats": candidate["repeats"], "runs": candidate["runs"],
                    "raw_score": candidate["p"], "score": round(candidate["score"], 4), "ci95": candidate["ci"],
                    "contaminated": candidate["contaminated"], "contamination_rate": candidate["contamination_rate"],
                    "candidate_count": len(cell["candidates"]), "eligible_candidate_count": len(patch),
                    "required_candidate_count": cell["publication"]["required_candidates"], "quality_passed": True,
                    "cost": cost, "dataset_sha256": bundle["dataset"]["sha256"], "rubric_sha256": bundle["rubric"]["sha256"],
                    "source_results_sha256": source_hashes["raw_results"], "score_report_sha256": source_hashes["score_report"],
                    "cell_evidence_sha256": cell["publication"]["evidence_sha256"],
                    "historical_registry_revision": None, "historical_registry_provenance": "not recorded; not backfilled from current export",
                    "baseline_identity": identity, "evaluated_at": report["scored_at"]}
        evidence["audited_results_sha256"] = source_hashes["audited_results"]
        evidence["audited_score_report_sha256"] = source_hashes["audited_score_report"]
        full = {"evidence": copy.deepcopy(evidence), "original_candidate_score": candidate, "original_items_sha256": digest(own),
                "ledger_rows_sha256": digest(attempts), "pending_reservations": pending,
                "quality_cost_separation": "No usage does not remove returned model outputs from quality scoring. All original rows retained; only originally contaminated rows excluded by frozen <=5% rule.",
                "scope_limit": "Historical BFCL component quality; no claim of a new runtime A/B comparison or total cost non-inferiority.",
                "promotion_requirement": "Dedicated capability-only Registry evidence contract must be deployed; no generic A/B schema or waiver"}
        save(folder / "report.json", full)
        evidence["report"] = str((folder / "report.json").resolve())
        evidence["report_sha256"] = file_digest(folder / "report.json")
        save(folder / "capability-evidence-proposal.json", evidence)
        promote_evidence = {
            "kind": "capability_scores", "policy": publication.POLICY_VERSION, "baseline_version": identity["version"],
            "report": evidence["report"], "report_sha256": evidence["report_sha256"],
            "evaluated_at": audited_report["scored_at"], "passed": True,
            "measurements": [{"measurement": patch[name][0], "items": candidate["items"], "repeats": candidate["repeats"],
                "complete_repeats": True, "raw_success_rate": candidate["p"], "ci95": candidate["ci"],
                "contamination_rate": candidate["contamination_rate"], "eligible_candidates": len(patch),
                "total_candidates": len(cell["candidates"]), "evidence_sha256": cell["publication"]["evidence_sha256"], "cost_known": True}],
        }
        from jsonschema import Draft202012Validator
        from referencing import Registry as SchemaRegistry, Resource
        resource_docs = [json.loads(p.read_text()) for p in args.promotion_schema.parent.glob("*.schema.json")]
        schema_registry = SchemaRegistry().with_resources((d["$id"], Resource.from_contents(d)) for d in resource_docs)
        Draft202012Validator(json.loads(args.promotion_schema.read_text()), registry=schema_registry,
                              format_checker=Draft202012Validator.FORMAT_CHECKER).validate(promote_evidence)
        save(folder / "promote-evaluation.json", promote_evidence)
        save(folder / "promote-request-template.json", {"kind": "REGISTRY_KIND_MODEL_PROFILE", "name": name,
             "version": "<PUBLISHED_VERSION>", "evaluation": {"schema_id": json.loads(args.promotion_schema.read_text())["$id"], "value": promote_evidence}})
        save(folder / "baseline.json", {"version": identity["version"], "registry_sha256": identity["sha256"], "document": baseline})
        save(folder / "publish.json", {"kind": "REGISTRY_KIND_MODEL_PROFILE", "name": name,
                                     "document": {"schema_id": MODEL_SCHEMA, "value": updated},
                                     "change_note": "MX-PUB act evidence " + evidence["report_sha256"] + "; capability_scores only; historical run round1"})
        bundle["items"].append({"profile_id": name, "directory": "candidates/" + slug,
                               "baseline_version": identity["version"], "baseline_registry_sha256": identity["sha256"],
                               "new_document_sha256": digest(updated), "quality_passed": True, "pricing_known": not pending,
                               "files": {f.name: file_digest(f) for f in folder.iterdir()}})
    save(args.out / "bundle.json", bundle)
    return bundle


class RemoteError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class Registry:
    def __init__(self, endpoint: str, tenant: str, token_file: Path | None):
        parsed = urllib.parse.urlsplit(endpoint)
        if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("invalid Registry endpoint")
        self.endpoint, self.tenant, self.token_file = endpoint.rstrip("/"), tenant, token_file

    def call(self, method: str, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if method == "Publish":
            if self.token_file is None:
                raise ValueError("AR_REGISTRY_PUBLISH_TOKEN_FILE is required")
            token = self.token_file.read_text().strip()
            if not token or any(c.isspace() for c in token):
                raise ValueError("invalid Registry publish token file")
            headers["Authorization"] = "Bearer " + token
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        request = urllib.request.Request(self.endpoint + SERVICE + method, json.dumps(body).encode(), headers, method="POST")
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            try:
                code = str(json.loads(exc.read()).get("code", "http_error"))
            except ValueError:
                code = "http_error"
            raise RemoteError(code if re.fullmatch(r"[a-z_]+", code) else "http_error") from None
        except (OSError, ValueError):
            raise RemoteError("response_unknown") from None

    def get(self, name: str, version: str = "") -> dict[str, Any]:
        return self.call("GetModelProfile", {"tenant": {"tenant_id": self.tenant}, "id": name, "version": version})["modelProfile"]


def publish_shadows(directory: Path, registry: Registry, *, execute: bool = False) -> dict[str, Any]:
    """Only the deployment coordinator calls execute during its publish window."""
    import fcntl
    with (directory / ".publication.lock").open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        bundle = json.loads((directory / "bundle.json").read_text())
        if (
            bundle.get("kind") != VERSION or bundle.get("publication_eligible_candidates") != len(bundle["items"])
            or not all(item.get("pricing_known") is True for item in bundle["items"])
        ):
            raise ValueError("bundle lacks the final cost-audited publication gate")
        state_path = directory / "publication-state.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {"bundle_sha256": file_digest(directory / "bundle.json"), "profiles": {}}
        if state["bundle_sha256"] != file_digest(directory / "bundle.json"):
            raise ValueError("publication bundle changed")
        for item in bundle["items"]:
            folder = directory / item["directory"]
            for name, expected in item["files"].items():
                if file_digest(folder / name) != expected:
                    raise ValueError("bundle file changed")
            baseline = json.loads((folder / "baseline.json").read_text())
            request = json.loads((folder / "publish.json").read_text())
            current = registry.get(item["profile_id"])
            if current["version"] != baseline["version"] or current["document"]["value"] != baseline["document"]:
                raise ValueError("default profile drifted; regenerate bundle before any mutation")
            previous = state["profiles"].get(item["profile_id"], {})
            found = None
            for number in range(int(baseline["version"][1:]) + 1, int(baseline["version"][1:]) + 1001):
                try:
                    version = registry.get(item["profile_id"], f"v{number}")
                except RemoteError as exc:
                    if exc.code == "not_found":
                        break
                    raise
                if version["document"]["value"] == request["document"]["value"]:
                    found = version["version"]
                    break
            else:
                raise ValueError("version scan bound exhausted")
            if found:
                state["profiles"][item["profile_id"]] = {"state": "shadow_found", "version": found}
                save(state_path, state)
                continue
            if previous.get("state") == "pending":
                raise ValueError("previous Publish response unknown; no matching version yet, reconcile before retry")
            if not execute:
                continue
            state["profiles"][item["profile_id"]] = {"state": "pending"}
            save(state_path, state)
            response = registry.call("Publish", request)
            version = response["version"]
            actual = registry.get(item["profile_id"], version)
            if actual["document"]["value"] != request["document"]["value"]:
                raise ValueError("published document differs; do not promote")
            state["profiles"][item["profile_id"]] = {"state": "published_shadow", "version": version}
            save(state_path, state)
        return state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("prepare")
    for name in ("run", "scores", "registry-export", "ledger", "dataset", "promotion-schema", "out"):
        build.add_argument("--" + name, type=Path, required=True)
    send = commands.add_parser("publish-shadows")
    send.add_argument("--bundle", type=Path, required=True)
    send.add_argument("--endpoint", required=True)
    send.add_argument("--tenant", default="evals")
    send.add_argument("--execute", action="store_true", help="coordinator only, after release window is fixed")
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare(args)
        print(json.dumps({"profiles": len(result["items"]), "pricing_known": sum(i["pricing_known"] for i in result["items"]),
                          "out": str(args.out), "published": False}))
    else:
        token = os.environ.get("AR_REGISTRY_PUBLISH_TOKEN_FILE")
        result = publish_shadows(args.bundle, Registry(args.endpoint, args.tenant, Path(token) if token else None), execute=args.execute)
        print(json.dumps({"execute": args.execute, "profiles": result["profiles"], "promoted": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
