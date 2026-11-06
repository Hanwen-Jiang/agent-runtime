"""FC-REAL reproducible host measurements, using the actual manager HTTP API.

preflight is read-only. run requires a coordinated resource-window ID and a
stable local manager PID; it never changes manager configuration or starts a
manager. All created VMs carry a unique owner/run label. Raw JSONL samples and
cleanup evidence are retained; a timeout is not retried as another create.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

DEFAULTS = {
    "AR_FC_MAX_VMS": "16", "AR_FC_MAX_VCPUS": "32", "AR_FC_MAX_MEMORY_MIB": "65536",
    "AR_FC_MAX_SCRATCH_MIB": "262144", "AR_FC_VM_DEFAULT_DISK_MIB": "4096", "AR_FC_SNAPSHOT_CACHE_MIB": "0", "AR_FC_WARM_POOL": "",
    "AR_FC_STATE_DIR": "/var/lib/agentruntime/fc/state", "AR_FC_IMAGES_DIR": "/var/lib/agentruntime/fc/images",
    "AR_FC_KERNEL": "/var/lib/agentruntime/fc/images/vmlinux", "AR_FC_CGROUP_ROOT": "/sys/fs/cgroup",
    "AR_FC_PARENT_CGROUP": "agentruntime-fc", "AR_FC_FIRECRACKER_BIN": "/usr/local/bin/firecracker",
    "AR_FC_LISTEN": "127.0.0.1:8097",
}
ENV_ID = re.compile(r"fc-[a-z0-9]+\Z")
RUN_LABEL = "fc-real/run"


class Failure(Exception):
    def __init__(self, code: str, status: int | None = None):
        self.code, self.status = code, status
        super().__init__(code)


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def configuration(path: Path) -> dict[str, str]:
    result = dict(DEFAULTS)
    for line in path.read_text("utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key in DEFAULTS:
            result[key] = value.strip().strip('"').strip("'")
    for key in ("AR_FC_MAX_VMS", "AR_FC_MAX_VCPUS", "AR_FC_MAX_MEMORY_MIB", "AR_FC_MAX_SCRATCH_MIB", "AR_FC_SNAPSHOT_CACHE_MIB"):
        if not result[key].isdigit():
            raise Failure("invalid_manager_configuration")
    return result


def records(cfg: dict[str, str]) -> dict[str, dict[str, Any]]:
    root = Path(cfg["AR_FC_STATE_DIR"]) / "vms"
    result = {}
    for path in root.glob("fc-*.json"):
        item = json.loads(path.read_text("utf-8"))
        if ENV_ID.fullmatch(item.get("env_id", "")):
            result[item["env_id"]] = item
    return result


def remaining_capacity(cfg: dict[str, str], existing: dict[str, dict[str, Any]], spec: dict[str, Any]) -> dict[str, int]:
    specs = [r["spec"] for r in existing.values()]
    return {
        "MAX_VMS": max(0, int(cfg["AR_FC_MAX_VMS"]) - len(specs)),
        "MAX_VCPUS": max(0, (int(cfg["AR_FC_MAX_VCPUS"]) - sum(int(s["vcpus"]) for s in specs)) // math.ceil(spec["cpu_millis"] / 1000)),
        "MAX_MEMORY_MIB": max(0, (int(cfg["AR_FC_MAX_MEMORY_MIB"]) - sum(int(s["memory_mib"]) for s in specs)) // spec["memory_mb"]),
        "MAX_SCRATCH_MIB": max(0, (int(cfg["AR_FC_MAX_SCRATCH_MIB"]) - sum(int(s["disk_mib"]) for s in specs)) // spec["disk_mb"]),
    }


def percentile(values: list[float], quantile: float) -> float | None:
    """Nearest-rank quantile, fixed before collecting measurements."""
    return sorted(values)[max(0, math.ceil(len(values) * quantile) - 1)] if values else None


def counters(path: Path) -> dict[str, int]:
    return {parts[0]: int(parts[1]) for line in path.read_text().splitlines() if len(parts := line.split()) == 2 and parts[1].isdigit()}


def host_resources(cfg: dict[str, str]) -> dict[str, Any]:
    memory = {p[0].rstrip(":"): int(p[1]) * 1024 for line in Path("/proc/meminfo").read_text().splitlines() if len(p := line.split()) >= 2}
    cpu = [int(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:9]]
    disks = {}
    for line in Path("/proc/diskstats").read_text().splitlines():
        p = line.split()
        if len(p) >= 14 and not p[2].startswith(("loop", "ram")):
            disks[p[2]] = {"read_bytes": int(p[5]) * 512, "write_bytes": int(p[9]) * 512, "io_ms": int(p[12])}
    pressure = {}
    for kind in ("cpu", "memory", "io"):
        path = Path("/proc/pressure") / kind
        pressure[kind] = path.read_text().strip() if path.exists() else None
    return {"monotonic_s": time.monotonic(), "cpu_ticks": cpu, "memory_total_bytes": memory.get("MemTotal"),
            "memory_available_bytes": memory.get("MemAvailable"), "swap_free_bytes": memory.get("SwapFree"),
            "disk_free_bytes": shutil.disk_usage(cfg["AR_FC_IMAGES_DIR"]).free, "disks": disks, "pressure": pressure}


def host_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    ticks = [b - a for a, b in zip(before["cpu_ticks"], after["cpu_ticks"], strict=True)]
    total = sum(ticks)
    return {"elapsed_s": after["monotonic_s"] - before["monotonic_s"],
            "cpu_busy_percent": 100 * (total - ticks[3] - ticks[4]) / total if total else None,
            "memory_available_change_bytes": after["memory_available_bytes"] - before["memory_available_bytes"],
            "disk_free_change_bytes": after["disk_free_bytes"] - before["disk_free_bytes"],
            "disks": {name: {k: v - before["disks"].get(name, {}).get(k, v) for k, v in values.items()} for name, values in after["disks"].items()}}


def vm_resources(cfg: dict[str, str], env_id: str) -> dict[str, Any]:
    if not ENV_ID.fullmatch(env_id):
        raise Failure("invalid_env_id")
    root = Path(cfg["AR_FC_CGROUP_ROOT"]) / cfg["AR_FC_PARENT_CGROUP"] / env_id
    result: dict[str, Any] = {}
    for name in ("cpu.stat", "memory.current", "memory.peak", "memory.events", "io.stat"):
        try:
            result[name] = (root / name).read_text().strip()
        except OSError as exc:
            result[name] = {"unavailable": type(exc).__name__}
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Failure("manager_redirect_refused", code)


class Manager:
    def __init__(self, url: str, token: str, timeout: float):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise Failure("invalid_manager_url")
        self.url, self._token, self.timeout = url.rstrip("/"), token.strip(), timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def call(self, method: str, path: str, owner: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.url + path, data=payload, method=method,
                                         headers={"Authorization": "Bearer " + self._token, "X-Sandbox-Owner": owner, "Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(1 << 20)
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            try:
                error = json.loads(exc.read(4096))
                code = error.get("error", error).get("code", "http_error")
            except (ValueError, AttributeError):
                code = "http_error"
            raise Failure(code if re.fullmatch(r"[a-z_]+", str(code)) else "http_error", exc.code) from None
        except (OSError, urllib.error.URLError) as exc:
            raise Failure("transport_" + type(exc).__name__.lower()) from None


def boot_source(created: dict[str, Any], before: dict[str, dict[str, Any]], cfg: dict[str, str], log: str) -> str:
    env_id = created["env_id"]
    if before.get(env_id, {}).get("owner") == "_pool":
        return "warm"
    if created.get("boot_source") in ("cold", "warm", "memory-snapshot", "cold-fallback"):
        return created["boot_source"]
    paths = re.findall(re.escape(env_id) + r" bootstrap path: ([a-z-]+)", log)
    if paths:
        if "snapshot bootstrap failed for " + env_id in log:
            return "cold-fallback"
        return paths[-1]
    if not cfg["AR_FC_WARM_POOL"] and int(cfg["AR_FC_SNAPSHOT_CACHE_MIB"]) == 0:
        return "cold"
    return "unknown"


def append(path: Path, item: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def cleanup(manager: Manager, manifest: dict[str, Any]) -> dict[str, Any]:
    owner, run_id = manifest["owner"], manifest["run_id"]
    deleted, failed = [], {}
    # Listing reconciles a POST that timed out after the server created the VM.
    try:
        environments = manager.call("GET", "/v1/envs", "_platform").get("environments", [])
    except Failure as exc:
        return {"complete": False, "deleted": [], "errors": {"reconcile": exc.code}}
    try:
        # Durable records include creates still booting, which GET omits.
        local = records(manifest["configuration"])
    except (OSError, ValueError, KeyError) as exc:
        return {"complete": False, "deleted": [], "errors": {"local_reconcile": type(exc).__name__}}
    environments = list({v["env_id"]: v for v in [*environments, *local.values()]}.values())
    pending_unknown = manifest.get("pending_create", False)
    for vm in environments:
        env_id = vm.get("env_id", "")
        if vm.get("owner") != owner or vm.get("labels", {}).get(RUN_LABEL) != run_id or not ENV_ID.fullmatch(env_id):
            continue
        if env_id not in manifest.get("known_ids", []):
            pending_unknown = False
        for attempt in range(3):
            try:
                manager.call("DELETE", "/v1/envs/" + env_id, owner)
                deleted.append(env_id)
                failed.pop(env_id, None)
                break
            except Failure as exc:
                failed[env_id] = exc.code
                if attempt < 2:
                    time.sleep(0.2 * (attempt + 1))
    try:
        remaining = [v["env_id"] for v in records(manifest["configuration"]).values()
                     if v.get("owner") == owner and v.get("labels", {}).get(RUN_LABEL) == run_id]
        if remaining:
            failed["remaining_records"] = remaining
    except (OSError, ValueError) as exc:
        failed["final_reconcile"] = type(exc).__name__
    if pending_unknown:
        failed["unknown_create"] = "request timed out without a reconciled VM; do not claim cleanup"
    return {"complete": not failed, "deleted": deleted, "errors": failed}


def preflight(args: argparse.Namespace, cfg: dict[str, str]) -> dict[str, Any]:
    reasons = []
    listen = urllib.parse.urlsplit("http://" + cfg["AR_FC_LISTEN"])
    target = urllib.parse.urlsplit(getattr(args, "manager_url", None) or "http://" + cfg["AR_FC_LISTEN"])
    allowed = {listen.hostname}
    if listen.hostname in ("0.0.0.0", "::"):
        allowed = {"127.0.0.1", "::1", "localhost"}
    if target.hostname not in allowed or target.port != listen.port:
        reasons.append("manager_url_does_not_match_effective_listen")
    for path in ("/dev/kvm", cfg["AR_FC_KERNEL"], str(Path(cfg["AR_FC_IMAGES_DIR"]) / (args.runtime + ".ext4")), cfg["AR_FC_FIRECRACKER_BIN"]):
        if not Path(path).exists():
            reasons.append("missing:" + path)
    if args.mode in ("cold", "density") and (cfg["AR_FC_WARM_POOL"] or int(cfg["AR_FC_SNAPSHOT_CACHE_MIB"])):
        reasons.append("cold_and_density_require_pool_and_snapshot_disabled")
    if args.mode == "warm":
        wanted = [args.runtime, str(args.cpu_millis), str(args.memory_mb)]
        matched = any(part.split(":")[:3] == wanted and len(part.split(":")) == 4 and part.split(":")[3].isdigit()
                      and int(part.split(":")[3]) > 0 for part in cfg["AR_FC_WARM_POOL"].split(","))
        if not matched or args.disk_mb != int(cfg["AR_FC_VM_DEFAULT_DISK_MIB"]):
            reasons.append("requested_spec_not_in_warm_pool")
    if args.mode == "snapshot" and (not int(cfg["AR_FC_SNAPSHOT_CACHE_MIB"]) or cfg["AR_FC_WARM_POOL"]):
        reasons.append("snapshot_requires_cache_enabled_and_pool_disabled")
    if args.mode == "snapshot":
        if not args.manager_log:
            reasons.append("snapshot_boot_evidence_log_required")
        elif not args.manager_log.is_file() or not os.access(args.manager_log, os.R_OK):
            reasons.append("snapshot_boot_evidence_log_unreadable")
    if not args.manager_pid:
        reasons.append("manager_pid_required_for_effective_configuration")
    else:
        try:
            entries = Path(f"/proc/{args.manager_pid}/environ").read_bytes().split(b"\0")
            actual = {k.decode(): v.decode() for e in entries if b"=" in e for k, v in [e.split(b"=", 1)] if k.decode() in DEFAULTS}
            if any(actual.get(k, DEFAULTS[k]) != v for k, v in cfg.items()):
                reasons.append("effective_manager_configuration_mismatch")
            if Path(f"/proc/{args.manager_pid}/comm").read_text().strip() != "fc-manager":
                reasons.append("process_is_not_fc_manager")
        except OSError:
            reasons.append("manager_process_unreadable")
    return {"ready": not reasons, "reasons": reasons, "configuration": cfg,
            "configuration_limits_are_not_physical_capacity": True, "vm_spec": vars_spec(args)}


def vars_spec(args: argparse.Namespace) -> dict[str, Any]:
    return {"runtime": args.runtime, "cpu_millis": args.cpu_millis, "memory_mb": args.memory_mb, "disk_mb": args.disk_mb}


def summarize(samples: list[dict[str, Any]], manifest: dict[str, Any], cleaned: dict[str, Any]) -> dict[str, Any]:
    priming = [s for s in samples if s.get("priming")]
    measured = [s for s in samples if not s.get("priming")]
    groups = {}
    for source in sorted({s.get("boot_source", "unknown") for s in measured}):
        rows = [s for s in measured if s.get("boot_source", "unknown") == source and s.get("outcome") == "completed"]
        values = [s["first_command_ms"] for s in rows]
        groups[source] = {"samples": len(rows), "p50_ms": percentile(values, 0.50), "p95_ms": percentile(values, 0.95),
                          "create_p50_ms": percentile([r["create_ms"] for r in rows if "create_ms" in r], .50),
                          "create_p95_ms": percentile([r["create_ms"] for r in rows if "create_ms" in r], .95)}
    expected = {"snapshot": "memory-snapshot", "warm": "warm", "cold": "cold", "density": "cold"}[manifest["mode"]]
    success = sum(s.get("outcome") == "completed" and s.get("boot_source") == expected for s in measured)
    telemetry_complete = all(all(isinstance(s.get("resources", {}).get(k), str)
                                 for k in ("cpu.stat", "memory.current", "memory.peak", "memory.events", "io.stat"))
                             and "host_delta" in s for s in measured if s.get("outcome") == "completed")
    return {"run_id": manifest["run_id"], "mode": manifest["mode"], "vm_spec": manifest["vm_spec"],
            "requested_samples": manifest["samples"], "measured_samples": len(measured), "by_boot_source": groups, "priming": priming,
            "failure_reasons": [s.get("failure") for s in samples if s.get("failure")],
            "configuration_limits": {k: v for k, v in manifest["configuration"].items() if k.startswith("AR_FC_MAX_")},
            "maximum_observed_concurrent": max((s.get("owned_concurrent", 0) for s in samples), default=0),
            "physical_capacity_conclusion": "lower_bound_only_no_physical_limit_established", "cleanup": cleaned,
            "resource_evidence_complete": telemetry_complete,
            "complete": success == manifest["samples"] and cleaned["complete"] and telemetry_complete, "actual_model_cost_usd": 0}


def run(args: argparse.Namespace, cfg: dict[str, str], manager: Manager) -> dict[str, Any]:
    checks = preflight(args, cfg)
    if not checks["ready"]:
        raise Failure("preflight_failed:" + ",".join(checks["reasons"]))
    if not re.fullmatch(r"[0-9a-f]{7,64}", args.runtime_revision):
        raise Failure("runtime_revision_required")
    if not args.resource_window_id:
        raise Failure("resource_window_id_required")
    out = args.out
    out.mkdir(parents=True, exist_ok=False)
    run_id = uuid.uuid4().hex
    manifest = {"run_id": run_id, "owner": "fc-real-" + run_id[:16], "mode": args.mode, "samples": args.samples,
                "vm_spec": vars_spec(args), "configuration": cfg, "resource_window_id": args.resource_window_id,
                "runtime_revision": args.runtime_revision, "started_at_unix": time.time(),
                "workload": "idle VM plus one shell command", "density_settle_s": args.settle_s,
                "host_page_cache": "image hashes read before samples; VM cold boot does not mean cold host page cache",
                "firecracker_sha256": digest(Path(cfg["AR_FC_FIRECRACKER_BIN"])), "kernel_sha256": digest(Path(cfg["AR_FC_KERNEL"])),
                "rootfs_sha256": digest(Path(cfg["AR_FC_IMAGES_DIR"]) / (args.runtime + ".ext4")),
                "manager_sha256": digest(Path(f"/proc/{args.manager_pid}/exe")), "host_cpu_count": os.cpu_count()}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    samples = []
    owner = manifest["owner"]
    live_ids = []
    manifest["known_ids"] = []
    manifest["pending_create"] = False
    try:
        # Snapshot population is a separate real create, explicitly excluded
        # from restore latency samples. No raw Firecracker snapshot API bypass.
        count = args.samples + (args.mode == "snapshot")
        for i in range(count):
            row: dict[str, Any] = {"index": i, "outcome": "failed", "priming": args.mode == "snapshot" and i == 0}
            existing = records(cfg)
            before = host_resources(cfg)
            row["host_before"] = before
            if before["memory_available_bytes"] < args.min_free_memory_mib * 1048576 + args.memory_mb * 1048576 or before["disk_free_bytes"] < args.min_free_disk_gib * 1073741824 + args.disk_mb * 1048576:
                row["failure"] = "host_safety_reserve_reached"
                append(out / "samples.jsonl", row)
                samples.append(row)
                break
            row["remaining_configuration_capacity"] = remaining_capacity(cfg, existing, manifest["vm_spec"])
            offset = args.manager_log.stat().st_size if args.manager_log and args.manager_log.exists() else 0
            try:
                start = time.monotonic()
                manifest["pending_create"] = True
                append(out / "requests.jsonl", {"phase": "creating", "known_ids": list(manifest["known_ids"]), "index": i})
                created = manager.call("POST", "/v1/envs", owner, {**manifest["vm_spec"], "labels": {RUN_LABEL: run_id}})
                env_id = created.get("env_id", "")
                if not ENV_ID.fullmatch(env_id):
                    raise Failure("invalid_create_response")
                row.update({"env_id": env_id, "create_ms": (time.monotonic() - start) * 1000})
                live_ids.append(env_id)
                manifest["known_ids"].append(env_id)
                manifest["pending_create"] = False
                append(out / "requests.jsonl", {"phase": "created", "known_ids": list(manifest["known_ids"]), "env_id": env_id, "index": i})
                execution = manager.call("POST", f"/v1/envs/{env_id}/agent/v1/exec", owner,
                                         {"argv": ["sh", "-c", "printf fc-real-ready"], "timeout_ms": 10000})
                row["first_command_ms"] = (time.monotonic() - start) * 1000
                if execution.get("exit_code") != 0 or base64.b64decode(execution.get("stdout", "")) != b"fc-real-ready":
                    raise Failure("guest_first_command_failed")
                log = ""
                if args.manager_log and args.manager_log.exists():
                    with args.manager_log.open("rb") as f:
                        f.seek(offset)
                        log = f.read(4 << 20).decode("utf-8", "replace")
                row["boot_source"] = boot_source(created, existing, cfg, log)
                if args.mode == "density":
                    time.sleep(args.settle_s)
                row["resources"] = vm_resources(cfg, env_id)
                row["host_before"], row["host_after"] = before, host_resources(cfg)
                row["host_delta"] = host_delta(before, row["host_after"])
                row["owned_concurrent"] = len(live_ids)
                row["outcome"] = "completed"
                if args.mode != "density":
                    manager.call("DELETE", "/v1/envs/" + env_id, owner)
                    live_ids.remove(env_id)
                if args.mode == "warm":
                    time.sleep(args.pool_wait_s)
            except (Failure, OSError, ValueError) as exc:
                code = exc.code if isinstance(exc, Failure) else type(exc).__name__
                row["failure"] = "configuration_limit" if code == "host_capacity" else code
                row["status"] = exc.status if isinstance(exc, Failure) else None
                if isinstance(exc, Failure) and exc.status is not None:
                    manifest["pending_create"] = False  # An explicit rejection is not an unknown create.
                    append(out / "requests.jsonl", {"phase": "rejected", "known_ids": list(manifest["known_ids"]), "index": i})
                # Keep host pressure at failures too; successful samples alone
                # cannot explain boot failures during a density run.
                try:
                    row["host_after"] = host_resources(cfg)
                    row["host_delta"] = host_delta(before, row["host_after"])
                    if row.get("env_id"):
                        row["resources"] = vm_resources(cfg, row["env_id"])
                except (OSError, ValueError) as telemetry_error:
                    row["resource_error"] = type(telemetry_error).__name__
            unexpected_restore = args.mode == "snapshot" and not row["priming"] and row.get("boot_source") != "memory-snapshot"
            if unexpected_restore and row["outcome"] == "completed":
                row["failure"] = "snapshot_restore_not_observed"
            append(out / "samples.jsonl", row)
            samples.append(row)
            if row["outcome"] != "completed" or unexpected_restore:
                break
    finally:
        cleaned = cleanup(manager, manifest)
        (out / "cleanup.json").write_text(json.dumps(cleaned, indent=2) + "\n")
    result = summarize(samples, manifest, cleaned)
    (out / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=("preflight", "run", "report", "cleanup"))
    p.add_argument("--config-file", type=Path, default=Path("/etc/agentruntime/fc-manager.env"))
    p.add_argument("--manager-url", help="defaults to effective AR_FC_LISTEN")
    p.add_argument("--manager-pid", type=int)
    p.add_argument("--manager-log", type=Path)
    p.add_argument("--token-file", type=Path, help="private file containing only the manager token; otherwise FC_REAL_TOKEN")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--mode", choices=("cold", "warm", "snapshot", "density"), default="cold")
    p.add_argument("--samples", type=int, default=20)
    p.add_argument("--runtime", default="python")
    p.add_argument("--cpu-millis", type=int, default=1000)
    p.add_argument("--memory-mb", type=int, default=512)
    p.add_argument("--disk-mb", type=int, default=256)
    p.add_argument("--min-free-memory-mib", type=int, default=8192)
    p.add_argument("--min-free-disk-gib", type=int, default=20)
    p.add_argument("--pool-wait-s", type=float, default=3.0)
    p.add_argument("--settle-s", type=float, default=1.0, help="density settling interval; idle VMs, no workload stress")
    p.add_argument("--timeout-s", type=float, default=90)
    p.add_argument("--resource-window-id")
    p.add_argument("--runtime-revision", default="unknown")
    args = p.parse_args(argv)
    try:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", args.runtime):
            raise Failure("invalid_runtime")
        if args.timeout_s <= 0 or args.pool_wait_s < 0 or args.settle_s < 0 or not all(math.isfinite(v) for v in (args.timeout_s, args.pool_wait_s, args.settle_s)):
            raise Failure("invalid_time_limits")
        if min(args.samples, args.cpu_millis, args.memory_mb, args.disk_mb, args.min_free_memory_mib, args.min_free_disk_gib) <= 0:
            raise Failure("positive_limits_required")
        if args.command == "report":
            result = json.loads((args.out / "summary.json").read_text())
        elif args.command == "preflight":
            result = preflight(args, configuration(args.config_file))
            args.out.mkdir(parents=True, exist_ok=True)
            (args.out / "preflight.json").write_text(json.dumps(result, indent=2) + "\n")
        else:
            token = args.token_file.read_text().strip() if args.token_file else os.environ.get("FC_REAL_TOKEN", "")
            if not token:
                raise Failure("manager_token_required")
            manager = Manager(args.manager_url or "http://" + configuration(args.config_file)["AR_FC_LISTEN"], token, args.timeout_s)
            if args.command == "cleanup":
                manifest = json.loads((args.out / "manifest.json").read_text())
                journal = args.out / "requests.jsonl"
                if journal.exists():
                    last = json.loads(journal.read_text().splitlines()[-1])
                    manifest["known_ids"] = last.get("known_ids", [])
                    manifest["pending_create"] = last.get("phase") == "creating"
                result = cleanup(manager, manifest)
                (args.out / "cleanup.json").write_text(json.dumps(result, indent=2) + "\n")
            else:
                import fcntl
                with Path("/tmp/agentruntime-fc-real.lock").open("a+b") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    result = run(args, configuration(args.config_file), manager)
        print(json.dumps(result, indent=2))
        return 0 if result.get("ready", result.get("complete", False)) else 4
    except (Failure, OSError, ValueError) as exc:
        print(json.dumps({"complete": False, "error": exc.code if isinstance(exc, Failure) else type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
