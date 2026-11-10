"""Format detection and deployed engine capabilities; never execute inputs."""
from __future__ import annotations
import importlib.util
import mimetypes
import os
import shutil
from pathlib import Path
from .models import Context, FileEngineError, engine_manifest

TYPES = {".pdf": "application/pdf", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
         ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
         ".ipynb": "application/x-ipynb+json", ".parquet": "application/vnd.apache.parquet"}


def media_type(path: Path) -> str:
    with path.open("rb") as stream:
        head = stream.read(32)
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    return TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def capabilities() -> dict:
    available = lambda name: importlib.util.find_spec(name) is not None
    model_dir = os.environ.get("FILE_ENGINES_MODEL_DIR", "")
    models = Path(model_dir) if model_dir else None
    rapid = models / "rapidocr" if models else None
    code_languages = {language: available("tree_sitter") and available(package) for language, package in (
        ("python", "tree_sitter_python"), ("javascript", "tree_sitter_javascript"),
        ("typescript", "tree_sitter_typescript"), ("tsx", "tree_sitter_typescript"),
        ("c", "tree_sitter_c"), ("go", "tree_sitter_go"), ("rust", "tree_sitter_rust"))}
    return {"text": True, "code_structure": any(code_languages.values()), "code_languages": code_languages,
            "pdf_text": available("pypdf"), "pdf_render": bool(shutil.which("pdftoppm")),
            "docx": available("docx"), "xlsx": available("openpyxl"), "pptx": available("pptx"),
            "image": available("PIL"), "ipynb": True, "archive": True, "parquet": available("duckdb"),
            "ocr": bool(available("rapidocr") and rapid and (rapid / "det.onnx").is_file() and (rapid / "rec.onnx").is_file()),
            "complex_pdf": bool(available("docling") and models and (models / "docling").is_dir()),
            "office_render": bool(shutil.which("soffice")), "executes_macros": False, "network_required": False}


def inspect_file(ctx: Context) -> dict:
    mime = media_type(ctx.source)
    outline, warnings = [], []
    if mime == "application/pdf":
        from pypdf import PdfReader
        reader = PdfReader(ctx.source)
        if reader.is_encrypted:
            raise FileEngineError("encrypted_document", "encrypted PDF requires a separately authorized decrypt step")
        outline = [{"kind": "pdf", "pages": len(reader.pages)}]
    elif mime.startswith("image/") and ctx.source.suffix.lower() != ".svg":
        from PIL import Image
        with Image.open(ctx.source) as image:
            outline = [{"kind": "image", "width": image.width, "height": image.height, "frames": getattr(image, "n_frames", 1)}]
    elif ctx.source.suffix.lower() in (".docx", ".xlsx", ".pptx"):
        from .parse import check_zip
        check_zip(ctx.source)
        outline = [{"kind": ctx.source.suffix[1:], "compressed_package": True}]
    return ctx.response(outline=outline, warnings=warnings, capabilities=capabilities(), coverage={"operation": "inspect", "content_read": False})
