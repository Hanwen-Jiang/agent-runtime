"""HTTP control-plane runner for on-demand Wave 2 self-evolution cycles (P1)."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import signal
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from agentruntime_learning.evolution.__main__ import DEFAULT_SKILLS_FILE, _open_store, _resolve_registry_creds
from agentruntime_learning.evolution.matrix import load_platform_skills_variants
from agentruntime_learning.evolution.pipeline import run_evolution_cycle
from agentruntime_learning.pipeline import stream

logger = logging.getLogger("evolution_runner")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _max_budget_usd() -> float:
    raw = os.environ.get("AR_EVOLUTION_MAX_BUDGET_USD", "1.0").strip()
    try:
        val = float(raw)
        return val if val >= 0 else 1.0
    except ValueError:
        return 1.0


class EvolutionRunManager:
    """Tracks background evolution runs on disk and serializes active execution."""

    def __init__(
        self,
        *,
        runs_dir: Path,
        skills_file: Path = DEFAULT_SKILLS_FILE,
        max_budget_usd: float = 1.0,
        executor: Callable[[dict[str, Any], list[str]], dict[str, Any]] | None = None,
    ) -> None:
        self.runs_dir = runs_dir
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.skills_file = skills_file
        self.max_budget_usd = max_budget_usd
        self._executor = executor or self._default_execute
        self._lock = threading.Lock()
        self._active_run_id: str | None = None

    def _run_path(self, run_id: str) -> Path:
        safe = "".join(ch for ch in run_id if ch.isalnum() or ch in ("-", "_"))
        return self.runs_dir / f"{safe}.json"

    def _save(self, rec: dict[str, Any]) -> None:
        path = self._run_path(rec["run_id"])
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)

    def list_runs(self, *, limit: int = 30) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in self.runs_dir.glob("*.json"):
            try:
                rows.append(json.loads(path.read_text("utf-8")))
            except Exception:  # noqa: BLE001
                continue
        rows.sort(key=lambda r: str(r.get("started_at") or ""), reverse=True)
        return rows[: max(1, limit)]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        path = self._run_path(run_id)
        if not path.is_file():
            return None
        return json.loads(path.read_text("utf-8"))

    def start_run(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        mode = str(payload.get("mode") or "fixture").strip()
        if mode not in ("fixture", "gateway"):
            return HTTPStatus.BAD_REQUEST, {"error": "mode must be fixture or gateway"}
        try:
            budget_usd = float(payload.get("budget_usd", 0.0))
        except (TypeError, ValueError):
            return HTTPStatus.BAD_REQUEST, {"error": "budget_usd must be numeric"}
        if budget_usd < 0 or budget_usd > self.max_budget_usd:
            return HTTPStatus.BAD_REQUEST, {"error": f"budget_usd must be between 0 and {self.max_budget_usd}"}
        try:
            min_support = max(1, int(payload.get("min_support", 2)))
        except (TypeError, ValueError):
            return HTTPStatus.BAD_REQUEST, {"error": "min_support must be an integer"}

        topic = str(payload.get("topic") or "telemetry.spans").strip()
        group = str(payload.get("group") or "ar-self-evolution").strip()
        fmt = str(payload.get("format") or ("console" if payload.get("traces") else "otlp_json")).strip()
        if fmt not in ("otlp_json", "console"):
            return HTTPStatus.BAD_REQUEST, {"error": "format must be otlp_json or console"}
        traces = payload.get("traces") or []
        if traces and not isinstance(traces, list):
            return HTTPStatus.BAD_REQUEST, {"error": "traces must be a list of file paths"}

        with self._lock:
            if self._active_run_id is not None:
                return HTTPStatus.CONFLICT, {"error": "another evolution run is already in progress", "active_run_id": self._active_run_id}
            run_id = f"evol-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
            self._active_run_id = run_id

        rec: dict[str, Any] = {
            "run_id": run_id,
            "status": "running",
            "mode": mode,
            "budget_usd": budget_usd,
            "min_support": min_support,
            "topic": topic,
            "group": group,
            "format": fmt,
            "traces": [str(t) for t in traces],
            "started_at": datetime.now(UTC).isoformat(),
            "finished_at": None,
            "summary": None,
            "logs": [f"queued evolution run {run_id} (mode={mode}, budget_usd={budget_usd}, min_support={min_support})"],
            "error": "",
        }
        self._save(rec)
        response_snapshot = {**rec, "logs": list(rec["logs"])}
        thread = threading.Thread(target=self._worker, args=(rec,), daemon=True)
        thread.start()
        return HTTPStatus.ACCEPTED, response_snapshot

    def _worker(self, rec: dict[str, Any]) -> None:
        logs = list(rec.get("logs") or [])
        try:
            summary = self._executor(rec, logs)
            rec["status"] = "completed"
            rec["summary"] = summary
            logs.append(f"completed with status={summary.get('status')} clusters={summary.get('clusters')}")
        except Exception as exc:  # noqa: BLE001
            rec["status"] = "failed"
            rec["error"] = f"{type(exc).__name__}: {exc}"
            logs.append(f"failed: {rec['error']}")
        finally:
            rec["logs"] = logs[-200:]
            rec["finished_at"] = datetime.now(UTC).isoformat()
            self._save(rec)
            with self._lock:
                if self._active_run_id == rec["run_id"]:
                    self._active_run_id = None

    def _default_execute(self, rec: dict[str, Any], logs: list[str]) -> dict[str, Any]:
        return asyncio.run(self._run_async(rec, logs))

    async def _run_async(self, rec: dict[str, Any], logs: list[str]) -> dict[str, Any]:
        class _NS:
            dsn = os.environ.get("AR_EVOLUTION_DSN", "")
            data_dir = os.environ.get("AR_DATA_DIR", "")
            registry_addr = os.environ.get("AR_REGISTRY_ADDRESS", "")
            publish_token = os.environ.get("AR_REGISTRY_PUBLISH_TOKEN", "")

        variants = load_platform_skills_variants(self.skills_file)
        by_name = {v.skill_name: v for v in variants}
        lines: list[str] = []
        reader = None
        snap = None
        traces = rec.get("traces") or []
        if traces:
            for p in traces:
                lines.extend(Path(p).read_text("utf-8").splitlines())
            logs.append(f"loaded {len(lines)} trace lines from {len(traces)} files")
        else:
            brokers = os.environ.get(stream.ENV_BROKERS, "").strip()
            if not brokers:
                raise RuntimeError(f"missing {stream.ENV_BROKERS} for topic {rec['topic']}")
            reader = stream.TopicReader(brokers, rec["topic"], rec["group"])
            snap = reader.snapshot()
            lines = snap.lines()
            logs.append(f"read {len(lines)} records from topic={snap.topic}")

        store, closeable = await _open_store(_NS())
        registry = None
        telemetry = None
        try:
            reg_addr, reg_tok = _resolve_registry_creds(_NS())
            if reg_addr and reg_tok:
                from agentruntime_worker.registry import GrpcRegistry

                registry = GrpcRegistry(reg_addr, publish_token=reg_tok)
            if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip():
                from agentruntime_core.adapters.otel import telemetry_from_env

                telemetry = telemetry_from_env("evolution-runner", os.environ)
            out_dir = self.runs_dir / rec["run_id"]
            res = await run_evolution_cycle(
                lines=lines,
                fmt=rec["format"],
                skills_by_name=by_name,
                out_dir=out_dir,
                store=store,
                registry=registry,
                telemetry=telemetry,
                mode=rec["mode"],
                min_support=int(rec["min_support"]),
                budget_usd=float(rec["budget_usd"]),
            )
            if reader is not None and snap is not None:
                reader.commit(snap)
            summary = res.summary()
            if snap is not None:
                summary["topic"] = snap.topic
                summary["records"] = len(snap.records)
            return summary
        finally:
            if reader is not None:
                reader.close()
            if registry is not None and hasattr(registry, "aclose"):
                await registry.aclose()
            if telemetry is not None and hasattr(telemetry, "aclose"):
                await telemetry.aclose()
            if closeable is not None and hasattr(closeable, "aclose"):
                await closeable.aclose()


def make_handler(manager: EvolutionRunManager, *, token: str) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        def _json(self, code: int, body: dict[str, Any]) -> None:
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _auth_ok(self) -> bool:
            if not token:
                return True
            auth = self.headers.get("Authorization", "")
            got = auth[7:].strip() if auth.startswith("Bearer ") else ""
            return hmac.compare_digest(got, token)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/healthz":
                self._json(HTTPStatus.OK, {"ok": True, "max_budget_usd": manager.max_budget_usd})
                return
            if not self._auth_ok():
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            if self.path == "/v1/runs":
                rows = manager.list_runs()
                self._json(HTTPStatus.OK, {"runs": rows, "count": len(rows), "max_budget_usd": manager.max_budget_usd})
                return
            if self.path.startswith("/v1/runs/"):
                run_id = self.path.removeprefix("/v1/runs/").strip("/")
                rec = manager.get_run(run_id)
                if rec is None:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "run not found"})
                    return
                self._json(HTTPStatus.OK, rec)
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/v1/runs":
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            if not self._auth_ok():
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            length = int(self.headers.get("Content-Length", "0") or "0")
            try:
                raw = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
                payload = json.loads(raw)
            except Exception:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
                return
            if not isinstance(payload, dict):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "JSON object required"})
                return
            code, body = manager.start_run(payload)
            self._json(code, body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    return _Handler


def main() -> None:
    addr = os.environ.get("AR_EVOLUTION_RUNNER_ADDR", "0.0.0.0:8099").strip()
    host, _, port_str = addr.rpartition(":")
    host = host or "0.0.0.0"
    port = int(port_str or "8099")
    token = os.environ.get("AR_EVOLUTION_RUNNER_TOKEN", "").strip()
    runs_dir = Path(os.environ.get("AR_EVOLUTION_RUNS_DIR", "/tmp/evolution-runs"))
    skills_file = Path(os.environ.get("AR_EVOLUTION_SKILLS_FILE", str(DEFAULT_SKILLS_FILE)))
    manager = EvolutionRunManager(runs_dir=runs_dir, skills_file=skills_file, max_budget_usd=_max_budget_usd())

    srv = ThreadingHTTPServer((host, port), make_handler(manager, token=token))

    def _shutdown(*_: Any) -> None:
        srv.shutdown()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    logger.info("evolution-runner listening on %s:%d (max_budget_usd=%.2f)", host, port, manager.max_budget_usd)
    srv.serve_forever()


if __name__ == "__main__":
    main()
