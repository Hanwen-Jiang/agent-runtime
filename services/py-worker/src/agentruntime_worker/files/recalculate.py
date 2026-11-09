"""LibreOffice UNO recalculation, separate from format conversion.

The sandbox image supplies /usr/bin/python3 + python3-uno and libreoffice-calc.
A private UNO pipe/profile is created per call; no existing desktop session is
used. This module also runs as the small system-Python UNO worker.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile


def _uno_worker(config_path: str) -> None:
    import uno
    from com.sun.star.beans import PropertyValue
    from com.sun.star.document.MacroExecMode import NEVER_EXECUTE
    from com.sun.star.document.UpdateDocMode import NO_UPDATE
    config = json.loads(Path(config_path).read_text())
    def prop(name, value):
        item = PropertyValue()
        item.Name, item.Value = name, value
        return item
    local = uno.getComponentContext()
    resolver = local.ServiceManager.createInstanceWithContext("com.sun.star.bridge.UnoUrlResolver", local)
    deadline = time.monotonic()+20
    while True:
        try:
            remote = resolver.resolve("uno:pipe,name="+config["pipe"]+";urp;StarOffice.ComponentContext")
            break
        except Exception:
            if time.monotonic() >= deadline:
                raise RuntimeError("UNO connection timeout")
            time.sleep(.1)
    desktop = remote.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", remote)
    document = desktop.loadComponentFromURL(Path(config["source"]).as_uri(), "_blank", 0,
        (prop("Hidden", True), prop("ReadOnly", False), prop("MacroExecutionMode", NEVER_EXECUTE), prop("UpdateDocMode", NO_UPDATE)))
    if document is None or not document.supportsService("com.sun.star.sheet.SpreadsheetDocument"):
        raise RuntimeError("Input is not a spreadsheet")
    try:
        document.enableAutomaticCalculation(True)
        document.calculateAll()
        errors = []
        for reference in config["formulas"]:
            sheet = document.Sheets.getByName(reference["sheet"])
            cell = sheet.getCellRangeByName(reference["cell"])
            error = cell.getError()
            if error:
                errors.append({**reference, "error": error})
        document.storeToURL(Path(config["output"]).as_uri(),
                            (prop("FilterName", "Calc MS Excel 2007 XML"), prop("Overwrite", True)))
        Path(config["report"]).write_text(json.dumps({"engine": "libreoffice-uno", "calculate_all": True, "formula_errors": errors}))
    finally:
        document.close(True)
        desktop.terminate()


def recalculate_workbook(source: Path, output: Path, options: dict | None = None) -> dict:
    from .models import FileEngineError
    import openpyxl
    options = options or {}
    source, output = Path(source).resolve(), Path(output).resolve()
    if source == output:
        raise FileEngineError("overwrite_original", "Recalculation must create a separate output version")
    with zipfile.ZipFile(source) as archive:
        if any(name.startswith("xl/externalLinks/") for name in archive.namelist()):
            raise FileEngineError("external_formula_dependency", "External workbook links require a separately authorized dependency snapshot")
    book = openpyxl.load_workbook(source, read_only=True, data_only=False, keep_links=False)
    try:
        formulas = [{"sheet": sheet.title, "cell": cell.coordinate, "formula": cell.value}
                    for sheet in book for row in sheet.iter_rows() for cell in row if cell.data_type == "f"]
    finally:
        book.close()
    executable = os.environ.get("AR_SOFFICE_BIN") or shutil.which("libreoffice") or shutil.which("soffice")
    python = os.environ.get("AR_UNO_PYTHON", "/usr/bin/python3")
    if not executable or not Path(python).exists():
        raise FileEngineError("recalculation_backend_missing", "A Linux LibreOffice Calc + python3-uno image is required for calculateAll")
    timeout = min(max(float(options.get("timeout_seconds", 60)), 5), 180)
    with tempfile.TemporaryDirectory(prefix="file-uno-") as temporary:
        directory = Path(temporary)
        pipe = "filecalc_"+uuid.uuid4().hex
        config = {"pipe": pipe, "source": str(source), "output": str(output), "formulas": formulas, "report": str(directory/"report.json")}
        config_path = directory/"request.json"
        config_path.write_text(json.dumps(config))
        with (directory/"soffice.log").open("wb") as log:
            process = subprocess.Popen([executable, "-env:UserInstallation="+(directory/"profile").as_uri(), "--headless", "--nologo", "--nodefault", "--nofirststartwizard", "--norestore", "--accept=pipe,name="+pipe+";urp;StarOffice.ServiceManager"], stdout=log, stderr=log)
            try:
                result = subprocess.run([python, str(Path(__file__).resolve()), "--uno-worker", str(config_path)], capture_output=True, text=True, timeout=timeout)
                if result.returncode:
                    raise FileEngineError("recalculation_failed", "UNO calculateAll failed: "+result.stderr[-600:])
            except subprocess.TimeoutExpired:
                raise FileEngineError("recalculation_timeout", "UNO calculateAll exceeded its time limit") from None
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
        report = json.loads((directory/"report.json").read_text())
    values = openpyxl.load_workbook(output, read_only=True, data_only=True, keep_links=False)
    expressions = openpyxl.load_workbook(output, read_only=True, data_only=False, keep_links=False)
    verified, missing = [], []
    try:
        for ref in formulas:
            value = values[ref["sheet"]][ref["cell"]]
            formula = expressions[ref["sheet"]][ref["cell"]]
            if formula.data_type != "f" or value.data_type == "e" or value.value is None:
                missing.append({"sheet": ref["sheet"], "cell": ref["cell"], "cached_value": value.value})
            verified.append({**ref, "saved_formula": formula.value, "evaluated_value": value.value, "calculation_status": "recalculated"})
        mismatches = []
        for expected in options.get("expected_values", []):
            actual = values[expected["sheet"]][expected["cell"]].value
            wanted = expected["value"]
            close = abs(actual-wanted) <= float(expected.get("tolerance", 1e-9)) if isinstance(actual, (int,float)) and isinstance(wanted, (int,float)) else actual == wanted
            if not close:
                mismatches.append({"sheet": expected["sheet"], "cell": expected["cell"], "expected": wanted, "actual": actual})
    finally:
        values.close()
        expressions.close()
    report.update(status="verified" if not (missing or mismatches or report["formula_errors"]) else "failed",
                  formula_count=len(formulas), missing_or_error_caches=missing, value_mismatches=mismatches,
                  cells=verified, output_sha256=hashlib.sha256(output.read_bytes()).hexdigest())
    return report


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--uno-worker":
    _uno_worker(sys.argv[2])


def process_recalculate(request: dict) -> dict:
    from .models import Context, FileEngineError
    context = Context.from_request(request)
    if context.source.suffix.lower() != ".xlsx":
        raise FileEngineError("unsupported_recalculation", "Explicit recalculation requires an XLSX workbook", status="unsupported")
    base = context.source_sha256
    if context.options.get("base_sha256") != base:
        raise FileEngineError("base_version_conflict", "Recalculation requires the exact source SHA-256")
    candidate = context.output("recalculated.xlsx")
    if candidate.exists():
        raise FileEngineError("artifact_conflict", "Use an isolated output directory for recalculation")
    report = recalculate_workbook(context.source, candidate, context.options)
    passed = report["status"] == "verified"
    criteria = {"operation": "calculateAll", "base_sha256": base, "expected_values": context.options.get("expected_values", [])}
    report.update(passed=passed, base_sha256=base, candidate_sha256=report["output_sha256"],
                  criteria_digest=hashlib.sha256(json.dumps(criteria, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                  fidelity="L1", render_verified=False)
    path = context.write_bytes("recalculation-report.json", json.dumps(report, ensure_ascii=False, indent=2, default=str).encode())
    mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    result = context.response(status="ready" if passed else "partial", artifacts=[context.artifact(candidate, mime), context.artifact(path, "application/json")],
                              warnings=[{"code": "engine_roundtrip", "message": "UNO recalculation re-saves the workbook; package-level L2 preservation is not claimed"}])
    result.update(candidate={"path": str(candidate), "sha256": report["output_sha256"], "size_bytes": candidate.stat().st_size, "mime_type": mime},
                  verification=report, required_fidelity="L1")
    return result
