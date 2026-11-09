"""Bounded, read-only table analysis inside the file-processing sandbox.

Only the selected source data is materialized in DuckDB. User SQL is never used
for ingestion, filenames, extension installation, or output paths.
"""
from __future__ import annotations

import csv
import datetime as dt
import decimal
import hashlib
import json
import re
import threading
from pathlib import Path
from typing import Any

from .models import Context, FileEngineError

MAX_CELLS = 2_000_000
MAX_RESULT_ROWS = 100_000
_DENIED_SQL = re.compile(r"\b(read_\w+|glob|http\w*|sqlite_scan|postgres_scan|query|query_table|getenv|current_setting)\s*\(", re.I)


def _json(value: Any) -> Any:
    if isinstance(value, (dt.date, dt.datetime, dt.time, decimal.Decimal)):
        return str(value)
    if isinstance(value, bytes):
        return {"binary_bytes": len(value)}
    return value


def _selection(context: Context, sheet: Any) -> tuple[int, int, int, int]:
    from openpyxl.utils.cell import range_boundaries
    region = context.selector.get("range")
    if region:
        try:
            bounds = range_boundaries(str(region))
        except (ValueError, TypeError):
            raise FileEngineError("invalid_selector", "Use an A1 cell range") from None
    else:
        bounds = (1, 1, max(sheet.max_column, 1), max(sheet.max_row, 1))
    left, top, right, bottom = bounds
    if any(v is None or v < 1 for v in bounds) or right < left or bottom < top:
        raise FileEngineError("invalid_selector", "Invalid cell range")
    if (right-left+1)*(bottom-top+1) > MAX_CELLS:
        raise FileEngineError("table_too_large", "Select at most 2 million cells")
    return left, top, right, bottom


def _xlsx(context: Context, connection: Any) -> tuple[dict, list[dict]]:
    import openpyxl
    source = openpyxl.load_workbook(context.source, read_only=True, data_only=False, keep_links=False)
    cached = openpyxl.load_workbook(context.source, read_only=True, data_only=True, keep_links=False)
    try:
        sheet_name = context.selector.get("sheet") or context.selector.get("sheet_name")
        if sheet_name is None:
            if len(source.sheetnames) != 1:
                raise FileEngineError("sheet_required", "Select a worksheet explicitly")
            sheet_name = source.sheetnames[0]
        if sheet_name not in source.sheetnames:
            raise FileEngineError("sheet_not_found", "Worksheet is not available")
        sheet, values = source[sheet_name], cached[sheet_name]
        left, top, right, bottom = _selection(context, sheet)
        rows = list(sheet.iter_rows(min_row=top, max_row=bottom, min_col=left, max_col=right))
        value_rows = list(values.iter_rows(min_row=top, max_row=bottom, min_col=left, max_col=right))
        formulas = []
        # A JSON relation permits type inference without converting every cell to
        # strings. Formulas always use their saved cache, never Python evaluation.
        column_names = []
        headers = bool(context.options.get("header", True))
        for index in range(right-left+1):
            title = str(rows[0][index].value or f"column_{index+1}") if headers and rows else f"column_{index+1}"
            name = title
            while name in column_names:
                name += "_"
            column_names.append(name)
        data = []
        for row_index, row in enumerate(rows):
            if row_index == 0 and headers:
                continue
            record = {}
            for col_index, cell in enumerate(row):
                value = cell.value
                if cell.data_type == "f":
                    value = value_rows[row_index][col_index].value
                    formulas.append({"cell": cell.coordinate, "formula": cell.value, "cached_value": _json(value),
                                     "evaluated_value": None, "calculation_status": "not_recalculated"})
                record[column_names[col_index]] = _json(value)
            data.append(record)
        if not data:
            connection.execute("CREATE TABLE data (" + ",".join('"'+name.replace('"','""')+'" VARCHAR' for name in column_names) + ")")
        else:
            # File is platform-generated, outside the source directory, and is
            # only read before external access is disabled for user SQL.
            materialized = context.output("selected-table.json")
            materialized.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False), encoding="utf-8")
            connection.execute("CREATE TABLE data AS SELECT * FROM read_json_auto(?)", [str(materialized)])
            materialized.unlink()
        return {"sheet": sheet_name, "range": context.selector.get("range"), "rows": len(data), "columns": column_names,
                "formula_cells": formulas, "calculation_status": "not_recalculated" if formulas else "not_applicable"}, formulas
    finally:
        source.close()
        cached.close()


def _load(context: Context, connection: Any) -> tuple[dict, list[dict]]:
    suffix = context.source.suffix.lower()
    if suffix == ".xlsx":
        return _xlsx(context, connection)
    functions = {".csv": "read_csv_auto", ".tsv": "read_csv_auto", ".parquet": "read_parquet", ".json": "read_json_auto", ".jsonl": "read_json_auto"}
    if suffix not in functions:
        raise FileEngineError("unsupported_table", "Table analysis supports CSV, TSV, JSON/JSONL, Parquet and XLSX", status="unsupported")
    extra = ", delim='\\t'" if suffix == ".tsv" else ""
    connection.execute(f"CREATE TABLE data AS SELECT * FROM {functions[suffix]}(?{extra})", [str(context.source)])
    # Apply declared row/column selectors before SQL is exposed, then remove the
    # unselected materialization so SQL cannot query a hidden full-data table.
    selected_columns = context.selector.get("columns")
    first = int(context.selector.get("row_offset", 0))
    count = context.selector.get("row_limit")
    columns = [row[0] for row in connection.execute("DESCRIBE data").fetchall()]
    if selected_columns is not None and (not isinstance(selected_columns, list) or not selected_columns or any(c not in columns for c in selected_columns)):
        raise FileEngineError("invalid_selector", "Unknown selected columns")
    if first < 0 or count is not None and int(count) < 0:
        raise FileEngineError("invalid_selector", "Row selectors must be nonnegative")
    if selected_columns is not None or first or count is not None:
        projection = ",".join('"'+c.replace('"','""')+'"' for c in selected_columns) if selected_columns else "*"
        limit = f" LIMIT {int(count)}" if count is not None else ""
        connection.execute(f"CREATE TABLE selected_data AS SELECT {projection} FROM data{limit} OFFSET {first}")
        connection.execute("DROP TABLE data")
        connection.execute("ALTER TABLE selected_data RENAME TO data")
    rows = connection.execute("SELECT count(*) FROM data").fetchone()[0]
    return {"rows": rows, "selector": context.selector, "calculation_status": "not_applicable"}, []


def process_compute(request: dict) -> dict:
    context = Context.from_request(request)
    try:
        import duckdb
    except ImportError:
        raise FileEngineError("missing_duckdb", "The table processor image must include DuckDB", status="unsupported") from None
    timeout = min(max(float(context.options.get("timeout_seconds", 30)), .1), 120)
    preview_limit = min(max(int(context.options.get("max_rows", 1000)), 1), 1000)
    result_limit = min(max(int(context.options.get("result_row_limit", MAX_RESULT_ROWS)), 1), MAX_RESULT_ROWS)
    connection = duckdb.connect(":memory:", config={"threads": "2", "memory_limit": "1GB", "autoload_known_extensions": "false", "autoinstall_known_extensions": "false"})
    timer = threading.Timer(timeout, connection.interrupt)
    timer.daemon = True
    timer.start()
    try:
        provenance, formulas = _load(context, connection)
        inputs=[{"alias":"data","source_sha256":context.source_sha256,"selector":context.selector}]
        tables=context.options.get("tables",[])
        if not isinstance(tables,list) or len(tables)>9:
            raise FileEngineError("table_limit","A query can use at most 10 authorized source tables")
        # Additional inputs are explicit mounted sources, never paths embedded
        # in SQL. Materialize only each declared selector before user execution.
        for index,table in enumerate(tables):
            alias=table.get("alias","")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}",alias) or alias.lower()=="data" or alias.lower() in {item["alias"].lower() for item in inputs}:
                raise FileEngineError("invalid_table_alias","Use distinct simple table aliases")
            child_output=context.output(f"table-{index}")
            child=Context.from_request({"operation":"table.query","path":table.get("path"),"input_root":str(context.input_root),"output_dir":str(child_output),"selector":table.get("selector",{}),"options":{"header":table.get("header",True)}})
            connection.execute('ALTER TABLE data RENAME TO "__primary_data"')
            child_provenance,child_formulas=_load(child,connection)
            connection.execute('ALTER TABLE data RENAME TO "'+alias+'"')
            connection.execute('ALTER TABLE "__primary_data" RENAME TO data')
            inputs.append({"alias":alias,"source_sha256":child.source_sha256,"selector":child.selector,"dataset":child_provenance})
            formulas.extend(child_formulas)
        connection.execute("SET enable_external_access=false")
        connection.execute("SET lock_configuration=true")
        schema = [{"name": row[0], "type": row[1], "nullable": row[2] == "YES"} for row in connection.execute("DESCRIBE data").fetchall()]
        operation = request.get("operation", "table.sample")
        if operation == "table.schema":
            result = context.response(coverage={"requested": context.selector, "processed": context.selector, "omitted": []})
            result.update(schema=schema, dataset=provenance)
            return result
        query = context.options.get("sql", "SELECT * FROM data")
        if not isinstance(query, str) or len(query) > 100_000 or _DENIED_SQL.search(query):
            raise FileEngineError("query_not_allowed", "Use a single SELECT over the authorized data table")
        statements = connection.extract_statements(query)
        if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
            raise FileEngineError("query_not_allowed", "Only one read-only SELECT is allowed")
        connection.execute(query, context.options.get("parameters", []))
        names = [item[0] for item in connection.description]
        target = context.output("query-result.csv")
        preview, count, truncated = [], 0, False
        preview_bytes=0
        with target.open("x", encoding="utf-8", newline="") as output:
            writer = csv.writer(output)
            writer.writerow(names)
            while True:
                batch = connection.fetchmany(min(1000, result_limit+1-count))
                if not batch:
                    break
                for row in batch:
                    if count >= result_limit:
                        truncated = True
                        break
                    converted = [_json(value) for value in row]
                    writer.writerow(converted)
                    row_bytes=len(json.dumps(converted,ensure_ascii=False,default=str))
                    if len(preview) < preview_limit and preview_bytes+row_bytes<=40_000:
                        preview.append(converted);preview_bytes+=row_bytes
                    count += 1
                if truncated:
                    break
        warnings = ([{"code": "formula_cache_only", "message": "Formula values are saved caches; this query did not recalculate the workbook"}] if formulas else [])
        if truncated:
            warnings.append({"code": "result_row_limit", "message": "The exported result is partial; total row count is unknown"})
        checks = {"input_rows": provenance["rows"], "returned_rows": count, "total_rows_known": not truncated, "read_only": True}
        result = context.response(status="partial" if truncated else "ready", warnings=warnings,
                                  coverage={"requested": context.selector, "processed": context.selector, "omitted": ["remaining_result_rows"] if truncated else []},
                                  artifacts=[context.artifact(target, "text/csv")])
        result.update(columns=names, rows=preview, schema=schema, dataset=provenance, verification=checks,
                      computation={"sql": query, "parameters": context.options.get("parameters", []), "engine": f"duckdb/{duckdb.__version__}",
                                   "source_sha256": context.source_sha256, "inputs": inputs, "selector": context.selector,
                                   "semantic_rules": context.options.get("semantic_rules", {})})
        return result
    except FileEngineError:
        raise
    except Exception as exc:
        raise FileEngineError("table_query_failed", f"{type(exc).__name__}: {str(exc)[:400]}") from None
    finally:
        timer.cancel()
        connection.close()
