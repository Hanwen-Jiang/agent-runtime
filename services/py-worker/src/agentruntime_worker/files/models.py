"""Bounded, serializable file-engine contract; independent of worker/Temporal."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

MAX_SOURCE_BYTES = 50 << 20
MAX_PAGES = 10
MAX_CELLS = 2000
MAX_BLOCKS = 500
MAX_TEXT_CHARS = 40_000
MAX_ARCHIVE_MEMBERS = 1000
MAX_EXPANDED_BYTES = 200 << 20
ENGINE_VERSION = "file-engines-v1"


class FileEngineError(Exception):
    def __init__(self, code: str, message: str, status: str = "failed"):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False).encode()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def engine_manifest() -> dict:
    packages = {}
    for name in ("pypdf", "docling", "rapidocr", "onnxruntime", "python-docx", "openpyxl", "python-pptx", "Pillow", "duckdb", "PyYAML", "defusedxml"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    sources = {p.name: sha256(p.read_bytes()) for p in sorted(Path(__file__).parent.glob("*.py"))}
    asset_paths = {"models": Path(os.environ.get("FILE_ENGINES_MODEL_DIR", "/nonexistent")) / "manifest.json", "fonts": Path("/opt/fonts.lock"), "system_packages": Path("/opt/system-packages.lock")}
    assets = {name: sha256(path.read_bytes()) if path.is_file() else None for name,path in asset_paths.items()}
    body = {"version": ENGINE_VERSION, "packages": packages, "sources": sources, "assets": assets}
    return {**body, "digest": sha256(canonical(body))}


def positive(value: Any, default: int, maximum: int, name: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise FileEngineError("invalid_selector", f"{name} must be between 1 and {maximum}")
    return value


def indices(selector: dict, total: int, *, singular="page_index", plural="pages", maximum=MAX_PAGES) -> tuple[list[int], bool]:
    values = selector.get(plural)
    if singular in selector:
        values = [selector[singular]]
    if values is None:
        start = selector.get("start", 0)
        count = positive(selector.get("count"), maximum, maximum, "count")
        if isinstance(start, bool) or not isinstance(start, int) or start < 0 or (total and start >= total):
            raise FileEngineError("invalid_selector", "start index is outside the document")
        values = list(range(start, min(total, start + count)))
    if not isinstance(values, list) or len(values) > maximum or any(isinstance(v, bool) or not isinstance(v, int) or not 0 <= v < total for v in values):
        raise FileEngineError("invalid_selector", "selection contains invalid or too many indices")
    return list(dict.fromkeys(values)), len(set(values)) < total


@dataclass
class Context:
    source: Path
    input_root: Path
    output_dir: Path
    selector: dict
    options: dict
    operation: str

    @classmethod
    def from_request(cls, request: dict) -> "Context":
        if not isinstance(request, dict):
            raise FileEngineError("invalid_request", "request must be a JSON object")
        for field in ("path", "input_root", "output_dir"):
            if not isinstance(request.get(field), str) or not request[field]:
                raise FileEngineError("invalid_path", f"{field} must be a nonempty path")
            path = Path(request[field])
            if not path.is_absolute() and not path.resolve().is_relative_to(Path.cwd().resolve()):
                raise FileEngineError("invalid_path", f"relative {field} must remain inside sandbox cwd")
        root = Path(request["input_root"]).resolve(strict=True)
        raw = Path(request["path"])
        source = raw.resolve(strict=True)
        if not source.is_relative_to(root) or not source.is_file() or source.is_symlink():
            raise FileEngineError("path_outside_input", "source must be a regular file within input_root")
        if source.stat().st_size > MAX_SOURCE_BYTES:
            raise FileEngineError("file_too_large", "source exceeds 50 MiB")
        output = Path(request["output_dir"]).resolve()
        if output == root or output == source or source.is_relative_to(output) or output.is_relative_to(root):
            raise FileEngineError("invalid_output", "output_dir must be separate from the immutable input tree")
        output.mkdir(parents=True, exist_ok=True)
        selector, options = request.get("selector") or {}, request.get("options") or {}
        if not isinstance(selector, dict) or not isinstance(options, dict):
            raise FileEngineError("invalid_request", "selector and options must be objects")
        context = cls(source, root, output, selector, options, str(request.get("operation", "inspect")))
        context.__dict__["source_sha256"] = sha256(source.read_bytes())
        return context

    @cached_property
    def source_sha256(self) -> str:
        return sha256(self.source.read_bytes())

    def output(self, relative: str) -> Path:
        raw = self.output_dir / relative
        path = raw.resolve()
        if not path.is_relative_to(self.output_dir) or path == self.output_dir or raw.is_symlink():
            raise FileEngineError("invalid_output", "artifact path is outside output_dir")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def write_bytes(self, relative: str, data: bytes) -> Path:
        path = self.output(relative)
        if path.exists():
            if not path.is_file() or path.read_bytes() != data:
                raise FileEngineError("artifact_conflict", "artifact already exists with different bytes")
            return path
        with path.open("xb") as stream:
            stream.write(data)
        return path

    def artifact(self, path: str | Path, mime_type: str, selector=None) -> dict:
        item = Path(path).resolve(strict=True)
        if not item.is_relative_to(self.output_dir) or not item.is_file() or item == self.source:
            raise FileEngineError("invalid_artifact", "artifact must be a file in output_dir")
        return {"path": str(item), "mime_type": mime_type, "selector": selector or {}, "sha256": sha256(item.read_bytes()), "size_bytes": item.stat().st_size}

    def block(self, kind: str, text: str, anchor: dict, **metadata) -> dict:
        identity = {"sha256": self.source_sha256, "kind": kind, "anchor": anchor}
        return {"id": "blk_" + sha256(canonical(identity))[:24], "kind": kind, "text": str(text), "anchor": anchor, **metadata}

    def response(self, status="ready", blocks=None, outline=None, warnings=None, coverage=None, artifacts=None, capabilities=None) -> dict:
        from .inspect import media_type
        if sha256(self.source.read_bytes()) != self.source_sha256:
            raise FileEngineError("source_modified", "file engine modified its immutable source")
        blocks = list(blocks or [])
        warnings = list(warnings or [])
        clipped = False
        used = 0
        kept = []
        for block in blocks[:MAX_BLOCKS]:
            room = MAX_TEXT_CHARS - used
            if room <= 0:
                clipped = True
                break
            value = dict(block)
            text = str(value.get("text", ""))
            if len(text) > room:
                value["text"] = text[:room]
                value["truncated"] = True
                clipped = True
            used += len(value.get("text", ""))
            kept.append(value)
        clipped |= len(kept) < len(blocks)
        if clipped:
            warnings.append({"code": "output_limit", "message": "output limited to 500 blocks / 40000 characters"})
            if status == "ready":
                status = "partial"
        coverage = dict(coverage or {})
        coverage.update({"returned_blocks": len(kept), "output_truncated": clipped})
        manifest = engine_manifest()
        return {"status": status, "blocks": kept, "outline": outline or [], "warnings": warnings, "coverage": coverage,
                "artifacts": artifacts or [], "capabilities": capabilities or {},
                "source": {"sha256": self.source_sha256, "size_bytes": self.source.stat().st_size, "mime_type": media_type(self.source)},
                "processor": {"version": ENGINE_VERSION, "digest": manifest["digest"], "packages": manifest["packages"], "assets": manifest["assets"],
                              "config_digest": sha256(canonical({"operation": self.operation, "selector": self.selector, "options": self.options}))}}
