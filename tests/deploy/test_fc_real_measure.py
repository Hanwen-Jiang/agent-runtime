"""Offline verification of FC-REAL attribution, accounting and safe cleanup.
These tests never start a VM and are not performance evidence.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/fc_real_measure.py"
spec = importlib.util.spec_from_file_location("fc_real_measure", SCRIPT)
assert spec and spec.loader
fc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fc)


def config(tmp_path):
    cfg = dict(fc.DEFAULTS)
    cfg["AR_FC_STATE_DIR"] = str(tmp_path)
    (tmp_path / "vms").mkdir(exist_ok=True)
    return cfg


def test_vcpu_limit_and_pool_occupancy_are_not_a_physical_density_limit(tmp_path):
    cfg = config(tmp_path)
    cfg["AR_FC_MAX_VMS"] = "64"
    existing = {"fc-123": {"owner": "_pool", "spec": {"vcpus": 2, "memory_mib": 2048, "disk_mib": 4096}}}
    limits = fc.remaining_capacity(cfg, existing, {"cpu_millis": 1000, "memory_mb": 256, "disk_mb": 256})
    assert limits["MAX_VMS"] == 63
    assert limits["MAX_VCPUS"] == 30
    assert min(limits.values()) == 30, "64 VM config cannot overcome the independent 32 vCPU config"


def test_boot_source_requires_evidence_and_never_uses_latency(tmp_path):
    cfg = config(tmp_path)
    cfg["AR_FC_WARM_POOL"] = "python:1000:512:1"
    created = {"env_id": "fc-abc", "create_ms": 0.001}
    assert fc.boot_source(created, {}, cfg, "") == "unknown"
    assert fc.boot_source(created, {"fc-abc": {"owner": "_pool"}}, cfg, "") == "warm"
    assert fc.boot_source(created, {}, cfg, "fc-abc bootstrap path: memory-snapshot") == "memory-snapshot"
    assert fc.boot_source(created, {}, cfg, "fc-other bootstrap path: memory-snapshot") == "unknown"
    assert fc.boot_source(created, {}, cfg, "snapshot bootstrap failed for fc-abc; fc-abc bootstrap path: cold-base-capture") == "cold-fallback"
    assert fc.boot_source(created, {}, cfg, "fc-abc bootstrap path: memory-snapshot\n从预热池交出 fc-abc（fc-real-own）") == "warm"
    assert fc.boot_source(created, {}, cfg, "从预热池交出 fc-abcd（fc-real-own）") == "unknown"


def test_snapshot_priming_is_excluded_but_fallbacks_are_retained(tmp_path):
    manifest = {"run_id": "test", "mode": "snapshot", "vm_spec": {}, "samples": 2, "configuration": config(tmp_path)}
    samples = [{"priming": True, "outcome": "completed", "boot_source": "cold-base-capture", "first_command_ms": 9999},
               {"outcome": "completed", "boot_source": "memory-snapshot", "first_command_ms": 15},
               {"outcome": "completed", "boot_source": "cold-fallback", "first_command_ms": 1000}]
    result = fc.summarize(samples, manifest, {"complete": True})
    assert result["measured_samples"] == 2 and len(result["priming"]) == 1
    assert result["by_boot_source"]["memory-snapshot"]["p50_ms"] == 15
    assert result["by_boot_source"]["cold-fallback"]["samples"] == 1
    assert not result["complete"]
    assert "no_physical_limit" in result["physical_capacity_conclusion"]
    assert fc.percentile(list(range(1, 21)), .95) == 19


class FakeManager:
    def __init__(self, items, cfg):
        self.items = items
        self.cfg = cfg
        self.deletes = []

    def call(self, method, path, owner, body=None):
        if method == "GET":
            return {"environments": self.items}
        assert method == "DELETE"
        env_id = path.rsplit("/", 1)[-1]
        self.deletes.append((owner, env_id))
        (Path(self.cfg["AR_FC_STATE_DIR"]) / "vms" / (env_id + ".json")).unlink(missing_ok=True)
        return {}


def test_cleanup_only_owns_run_label_and_tenant_and_reconciles_booting_vm(tmp_path):
    cfg = config(tmp_path)
    owned = {"env_id": "fc-owned", "owner": "fc-real-own", "labels": {fc.RUN_LABEL: "run"}}
    strangers = [{"env_id": "fc-other", "owner": "other", "labels": {fc.RUN_LABEL: "run"}},
                 {"env_id": "fc-sibling", "owner": "fc-real-own", "labels": {fc.RUN_LABEL: "other"}}]
    # A booting VM is absent from GET but already has a durable record.
    (tmp_path / "vms" / "fc-owned.json").write_text(json.dumps(owned))
    manager = FakeManager(strangers, cfg)
    result = fc.cleanup(manager, {"owner": "fc-real-own", "run_id": "run", "configuration": cfg, "pending_create": True, "known_ids": []})
    assert result["complete"] and manager.deletes == [("fc-real-own", "fc-owned")]


def test_unreconciled_create_timeout_is_never_reported_clean(tmp_path):
    cfg = config(tmp_path)
    result = fc.cleanup(FakeManager([], cfg), {"owner": "fc-real-own", "run_id": "run", "configuration": cfg,
                                              "pending_create": True, "known_ids": []})
    assert not result["complete"] and "unknown_create" in result["errors"]


def test_configuration_export_omits_credentials_and_never_executes_shell(tmp_path):
    file = tmp_path / "env"
    file.write_text("AR_FC_MAX_VMS=64\nAR_SECRETS_FILE=/private/key\nAR_DEV_SIGNING_KEY=secret\nUNUSED=$(touch /tmp/forbidden)\n")
    cfg = fc.configuration(file)
    assert cfg["AR_FC_MAX_VMS"] == "64"
    assert "secret" not in json.dumps(cfg) and "AR_SECRETS_FILE" not in cfg


def test_resource_cpu_and_io_deltas_have_explicit_units():
    before = {"monotonic_s": 1, "cpu_ticks": [10, 0, 10, 80, 0, 0, 0, 0], "memory_available_bytes": 1000,
              "disk_free_bytes": 2000, "disks": {"sda": {"read_bytes": 100, "write_bytes": 0, "io_ms": 2}}}
    after = {"monotonic_s": 2, "cpu_ticks": [40, 0, 30, 130, 0, 0, 0, 0], "memory_available_bytes": 900,
             "disk_free_bytes": 1800, "disks": {"sda": {"read_bytes": 120, "write_bytes": 200, "io_ms": 12}}}
    result = fc.host_delta(before, after)
    assert result["cpu_busy_percent"] == 50
    assert result["memory_available_change_bytes"] == -100
    assert result["disks"]["sda"] == {"read_bytes": 20, "write_bytes": 200, "io_ms": 10}


def test_preflight_warm_requires_matching_disk_and_snapshot_requires_evidence(tmp_path):
    cfg = config(tmp_path)
    cfg["AR_FC_WARM_POOL"] = "python:1000:512:2"
    args = SimpleNamespace(mode="warm", runtime="python", cpu_millis=1000, memory_mb=512, disk_mb=256, manager_pid=None, manager_log=None)
    assert "requested_spec_not_in_warm_pool" in fc.preflight(args, cfg)["reasons"]
    args.mode = "snapshot"
    result = fc.preflight(args, cfg)
    assert "snapshot_requires_cache_enabled_and_pool_disabled" in result["reasons"]
    assert "snapshot_boot_evidence_log_required" in result["reasons"]


def test_runner_uses_existing_api_and_cleans_only_its_created_vm(tmp_path, monkeypatch):
    import base64
    (tmp_path / "state").mkdir()
    cfg = config(tmp_path / "state")
    args = SimpleNamespace(out=tmp_path / "run", mode="cold", samples=1, runtime="python", cpu_millis=1000,
                           memory_mb=512, disk_mb=256, manager_pid=123, resource_window_id="offline-test",
                           runtime_revision="1234567", min_free_memory_mib=1, min_free_disk_gib=1,
                           manager_log=None, pool_wait_s=0, settle_s=0)
    monkeypatch.setattr(fc, "preflight", lambda *a: {"ready": True})
    monkeypatch.setattr(fc, "digest", lambda *a: "sha256")
    monkeypatch.setattr(fc, "vm_resources", lambda *a: {k: "0" for k in ("cpu.stat", "memory.current", "memory.peak", "memory.events", "io.stat")})
    monkeypatch.setattr(fc, "host_resources", lambda *a: {"memory_available_bytes": 20 << 30, "disk_free_bytes": 50 << 30})
    monkeypatch.setattr(fc, "host_delta", lambda *a: {"cpu_busy_percent": 0})
    calls = []

    class API:
        def call(self, method, path, owner, body=None):
            calls.append((method, path, owner))
            if method == "POST" and path == "/v1/envs":
                assert body["labels"][fc.RUN_LABEL]
                return {"env_id": "fc-abc"}
            if method == "POST":
                assert path == "/v1/envs/fc-abc/agent/v1/exec"
                return {"exit_code": 0, "stdout": base64.b64encode(b"fc-real-ready").decode()}
            if method == "GET":
                return {"environments": []}
            assert method == "DELETE" and path == "/v1/envs/fc-abc"
            return {}

    result = fc.run(args, cfg, API())
    assert result["complete"] and result["by_boot_source"]["cold"]["samples"] == 1
    assert sum(method == "POST" and path == "/v1/envs" for method, path, _ in calls) == 1
    assert (args.out / "cleanup.json").is_file() and (args.out / "requests.jsonl").is_file()
