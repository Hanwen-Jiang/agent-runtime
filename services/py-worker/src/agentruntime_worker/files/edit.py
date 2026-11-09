"""Targeted, immutable Office/Notebook editing with explicit verification.

OOXML is patched at selected XML nodes; untouched ZIP members retain identical
uncompressed bytes. A package-preservation check is not a visual-fidelity claim.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import posixpath
import time
import zipfile
from typing import Any

from .models import Context, FileEngineError

NS = {"w":"http://schemas.openxmlformats.org/wordprocessingml/2006/main",
      "s":"http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r":"http://schemas.openxmlformats.org/officeDocument/2006/relationships",
      "a":"http://schemas.openxmlformats.org/drawingml/2006/main",
      "p":"http://schemas.openxmlformats.org/presentationml/2006/main",
      "rel":"http://schemas.openxmlformats.org/package/2006/relationships"}
MIME = {".docx":"application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xlsx":"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".pptx":"application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".ipynb":"application/x-ipynb+json"}


def _digest(value: Any) -> str:
    data = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()


def _package(source: Path) -> tuple[dict[str, bytes], list]:
    with zipfile.ZipFile(source) as archive:
        infos = archive.infolist()
        if len(infos)>10_000 or sum(info.file_size for info in infos)>200<<20:
            raise FileEngineError("package_limit", "Office package exceeds member or expanded-size limit")
        if len({i.filename for i in infos}) != len(infos):
            raise FileEngineError("invalid_package", "Duplicate package members are not supported")
        for info in infos:
            path = PurePosixPath(info.filename)
            if path.is_absolute() or ".." in path.parts or "\\" in info.filename or info.flag_bits&1:
                raise FileEngineError("invalid_package", "Unsafe or encrypted Office package member")
        return {info.filename: archive.read(info) for info in infos}, infos


def _parse(raw: bytes):
    from lxml import etree
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise FileEngineError("xml_entity_not_allowed", "Office XML must not contain entity declarations")
    return etree.fromstring(raw, etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False))


def _serialize(root) -> bytes:
    from lxml import etree
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _index(values: list, number: Any, label: str):
    try:
        index = int(number)
    except (ValueError, TypeError):
        raise FileEngineError("invalid_selector", f"{label} must be a zero-based index") from None
    if index < 0 or index >= len(values):
        raise FileEngineError("target_not_found", f"{label} does not exist")
    return values[index]


def _text(node, value: Any) -> None:
    if not isinstance(value, str):
        raise FileEngineError("invalid_edit", "Replacement text must be a string")
    node.text = value
    node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")


def _replace(nodes: list, old: str, new: str, count: int) -> int:
    if not old or not isinstance(new, str):
        raise FileEngineError("invalid_edit", "Provide nonempty expected_text and string text")
    joined = "".join(node.text or "" for node in nodes)
    occurrences = joined.count(old)
    if occurrences != count:
        raise FileEngineError("edit_precondition_failed", f"Expected {count} exact text matches, found {occurrences}")
    # Work from the end so earlier run boundaries do not move. Text outside each
    # selected match remains in its original run with its original formatting.
    positions = [match.start() for match in re.finditer(re.escape(old), joined)]
    for start in reversed(positions):
        end, offset, inserted = start+len(old), 0, False
        for node in nodes:
            text = node.text or ""
            low, high = max(start-offset,0), min(end-offset,len(text))
            if low < high:
                _text(node, text[:low]+(new if not inserted else "")+text[high:])
                inserted = True
            offset += len(text)
    return occurrences


def _docx(parts: dict[str,bytes], operations: list[dict], selector: dict) -> list[dict]:
    root = _parse(parts["word/document.xml"])
    paragraphs = root.xpath("./w:body/w:p", namespaces=NS)
    changes = []
    for op in operations:
        if op.get("type")=="set_table_cell_text":
            from lxml import etree
            table_index,row_index,column_index=op.get("table_index"),op.get("row_index"),op.get("column_index")
            if selector.get("paragraph_indices") is not None or selector.get("table_index") is not None and selector["table_index"]!=table_index:
                raise FileEngineError("selector_denied","Table lies outside the authorized selection")
            table=_index(root.xpath("./w:body/w:tbl",namespaces=NS),table_index,"table_index")
            row=_index(table.xpath("./w:tr",namespaces=NS),row_index,"row_index")
            cell=_index(row.xpath("./w:tc",namespaces=NS),column_index,"column_index")
            if table.xpath(".//w:gridSpan|.//w:vMerge",namespaces=NS):raise FileEngineError("complex_table","Merged tables require an explicit grid-aware editor")
            if cell.xpath(".//w:ins|.//w:del|.//w:fldChar",namespaces=NS):raise FileEngineError("complex_target","Tracked changes and fields require a dedicated editor")
            nodes=cell.xpath(".//w:t",namespaces=NS);before="".join(n.text or "" for n in nodes)
            if op.get("expected_text")!=before:raise FileEngineError("edit_precondition_failed","Table cell text changed")
            if before:_replace(nodes,before,op.get("text"),1)
            elif nodes:_text(nodes[0],op.get("text"))
            else:
                paragraph=cell.find("w:p",NS)
                if paragraph is None:paragraph=etree.SubElement(cell,"{"+NS["w"]+"}p")
                _text(etree.SubElement(etree.SubElement(paragraph,"{"+NS["w"]+"}r"),"{"+NS["w"]+"}t"),op.get("text"))
            changes.append({"table_index":table_index,"row_index":row_index,"column_index":column_index,"before":before,"after":op["text"]})
            continue
        index = op.get("paragraph_index")
        allowed = selector.get("paragraph_indices")
        if allowed is not None and index not in allowed:
            raise FileEngineError("selector_denied", "Edit exceeds the authorized paragraph selection")
        paragraph = _index(paragraphs,index,"paragraph_index")
        # Revision/drawing semantics are outside this operation's preservation
        # contract. Never flatten these into ordinary paragraph text.
        if paragraph.xpath(".//w:ins|.//w:del|.//w:fldChar",namespaces=NS):
            raise FileEngineError("complex_target", "Tracked changes and fields require a dedicated editor")
        nodes = paragraph.xpath(".//w:t",namespaces=NS)
        before = "".join(node.text or "" for node in nodes)
        if op.get("type") == "replace_text":
            _replace(nodes,op.get("expected_text",""),op.get("text"),int(op.get("occurrences",1)))
        elif op.get("type") == "set_run_text":
            node = _index(nodes,op.get("run_index"),"run_index")
            if "expected_text" in op and node.text != op["expected_text"]:
                raise FileEngineError("edit_precondition_failed", "Run text changed")
            _text(node,op.get("text"))
        else:
            raise FileEngineError("unsupported_edit", "DOCX supports replace_text and set_run_text",status="unsupported")
        changes.append({"paragraph_index":index,"before":before,"after":"".join(n.text or "" for n in nodes)})
    parts["word/document.xml"] = _serialize(root)
    return changes


def _xlsx(parts: dict[str,bytes], operations: list[dict], selector: dict) -> list[dict]:
    from lxml import etree
    from openpyxl.utils.cell import coordinate_to_tuple, range_boundaries
    workbook = _parse(parts["xl/workbook.xml"])
    relationships = _parse(parts["xl/_rels/workbook.xml.rels"])
    targets = {r.get("Id"):r.get("Target") for r in relationships}
    sheets = {}
    for sheet in workbook.xpath("./s:sheets/s:sheet",namespaces=NS):
        target = targets[sheet.get("{"+NS["r"]+"}id")]
        sheets[sheet.get("name")] = target.lstrip("/") if target.startswith("/") else "xl/"+target
    loaded, changes = {}, []
    for op in operations:
        sheet_name, coordinate = op.get("sheet"),str(op.get("cell","")).upper()
        if selector.get("sheet") and sheet_name != selector["sheet"]:
            raise FileEngineError("selector_denied", "Edit exceeds the authorized worksheet")
        if sheet_name not in sheets or not re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]{0,6}",coordinate):
            raise FileEngineError("invalid_selector", "Specify an existing worksheet and A1 cell")
        row_number,column = coordinate_to_tuple(coordinate)
        if row_number>1048576 or column>16384:
            raise FileEngineError("invalid_selector", "Cell exceeds workbook limits")
        if selector.get("range"):
            left,top,right,bottom = range_boundaries(selector["range"])
            if not(left<=column<=right and top<=row_number<=bottom):
                raise FileEngineError("selector_denied", "Cell lies outside the authorized range")
        name = sheets[sheet_name]
        root = loaded.setdefault(name,_parse(parts[name]))
        cells = root.xpath(".//s:sheetData/s:row/s:c[@r=$cell]",namespaces=NS,cell=coordinate)
        if cells:
            cell=cells[0]
        else:
            rows=root.xpath("./s:sheetData/s:row[@r=$row]",namespaces=NS,row=str(row_number))
            data=root.find("s:sheetData",NS)
            if data is None:
                raise FileEngineError("invalid_workbook", "Worksheet has no sheetData")
            if rows:
                row=rows[0]
            else:
                row=etree.Element("{"+NS["s"]+"}row",r=str(row_number))
                following=next((r for r in data if int(r.get("r","0"))>row_number),None)
                if following is None:data.append(row)
                else:data.insert(data.index(following),row)
            cell=etree.Element("{"+NS["s"]+"}c",r=coordinate)
            following=next((c for c in row if coordinate_to_tuple(c.get("r"))[1]>column),None)
            if following is None:row.append(cell)
            else:row.insert(row.index(following),cell)
        before=_serialize(cell).decode()
        if op.get("type") not in {"set_cell","set_formula"}:
            raise FileEngineError("unsupported_edit","XLSX supports set_cell and set_formula",status="unsupported")
        if cell.find("s:f",NS) is not None and cell.find("s:f",NS).get("t") in {"shared","array","dataTable"}:
            raise FileEngineError("complex_formula","Shared/array/data-table formulas require a range-aware editor")
        for child in list(cell):
            if child.tag in {"{"+NS["s"]+"}"+tag for tag in ("f","v","is")}:
                cell.remove(child)
        cell.attrib.pop("t",None)
        value=op.get("value")
        if op["type"]=="set_formula":
            formula=op.get("formula","")
            if not isinstance(formula,str) or not formula.startswith("=") or len(formula)<2 or "[" in formula:
                raise FileEngineError("invalid_formula","Use an internal formula beginning with =; external references are not supported")
            etree.SubElement(cell,"{"+NS["s"]+"}f").text=formula[1:]
        elif isinstance(value,bool):
            cell.set("t","b");etree.SubElement(cell,"{"+NS["s"]+"}v").text="1" if value else "0"
        elif isinstance(value,(int,float)):
            if not math.isfinite(value):raise FileEngineError("invalid_value","Cell numbers must be finite")
            etree.SubElement(cell,"{"+NS["s"]+"}v").text=str(value)
        elif isinstance(value,str):
            cell.set("t","inlineStr");_text(etree.SubElement(etree.SubElement(cell,"{"+NS["s"]+"}is"),"{"+NS["s"]+"}t"),value)
        elif value is not None:
            raise FileEngineError("invalid_value","Use a string, number, boolean or null cell value")
        changes.append({"sheet":sheet_name,"cell":coordinate,"before_xml":before,"value":value,"formula":op.get("formula")})
    # Every formula cache may depend on edited cells. Clear them explicitly,
    # rather than accidentally returning stale data as newly evaluated values.
    for name in sheets.values():
        root=loaded[name] if name in loaded else _parse(parts[name])
        altered=name in loaded
        for cell in root.xpath(".//s:c[s:f]",namespaces=NS):
            value=cell.find("s:v",NS)
            if value is not None:cell.remove(value);altered=True
        if name in loaded:
            coordinates=[coordinate_to_tuple(c.get("r")) for c in root.xpath(".//s:sheetData/s:row/s:c",namespaces=NS)]
            dimension=root.find("s:dimension",NS)
            if coordinates and dimension is not None:
                from openpyxl.utils.cell import get_column_letter
                r1,c1=min(r for r,c in coordinates),min(c for r,c in coordinates)
                r2,c2=max(r for r,c in coordinates),max(c for r,c in coordinates)
                dimension.set("ref",f"{get_column_letter(c1)}{r1}:{get_column_letter(c2)}{r2}")
        if altered:parts[name]=_serialize(root)
    calculation=workbook.find("s:calcPr",NS)
    if calculation is None:calculation=etree.SubElement(workbook,"{"+NS["s"]+"}calcPr")
    calculation.set("fullCalcOnLoad","1");calculation.set("forceFullCalc","1");calculation.set("calcMode","auto")
    parts["xl/workbook.xml"]=_serialize(workbook)
    if "xl/calcChain.xml" in parts:
        del parts["xl/calcChain.xml"]
        for relationship in list(relationships):
            if relationship.get("Type","").endswith("/calcChain"):relationships.remove(relationship)
        parts["xl/_rels/workbook.xml.rels"]=_serialize(relationships)
        content=_parse(parts["[Content_Types].xml"])
        for node in list(content):
            if node.get("PartName")=="/xl/calcChain.xml":content.remove(node)
        parts["[Content_Types].xml"]=_serialize(content)
    return changes


def _pptx(parts: dict[str,bytes],operations: list[dict],selector: dict,context: Context) -> list[dict]:
    loaded,changes={},[]
    presentation=_parse(parts["ppt/presentation.xml"])
    presentation_rels=_parse(parts["ppt/_rels/presentation.xml.rels"])
    targets={node.get("Id"):node.get("Target") for node in presentation_rels}
    slide_names=[]
    for slide in presentation.xpath("./p:sldIdLst/p:sldId",namespaces=NS):
        target=targets[slide.get("{"+NS["r"]+"}id")]
        name=target.lstrip("/") if target.startswith("/") else posixpath.normpath("ppt/"+target)
        if not name.startswith("ppt/slides/"):raise FileEngineError("invalid_package","Unexpected slide relationship")
        slide_names.append(name)
    for op in operations:
        index=int(op.get("slide_index",-1))
        if index<0 or selector.get("slide_indices") is not None and index not in selector["slide_indices"]:
            raise FileEngineError("selector_denied","Slide is outside the authorized selection")
        name=_index(slide_names,index,"slide_index")
        if name not in parts:raise FileEngineError("target_not_found","Slide does not exist")
        root=loaded.setdefault(name,_parse(parts[name]))
        if op.get("type")=="replace_image":
            from lxml import etree
            from PIL import Image
            raw_path=Path(str(op.get("image_path","")))
            image_path=raw_path.resolve(strict=True)
            if not image_path.is_relative_to(context.input_root) or not image_path.is_file():
                raise FileEngineError("selector_denied","Replacement image must be an explicitly mounted input")
            if image_path.stat().st_size>10<<20:raise FileEngineError("image_too_large","Replacement image exceeds 10 MiB")
            with Image.open(image_path) as image:
                if image.format not in {"PNG","JPEG"} or image.width*image.height>20_000_000:raise FileEngineError("invalid_image","Use a bounded PNG or JPEG replacement")
                extension="png" if image.format=="PNG" else "jpeg"
                image.verify()
            pictures=root.xpath(".//p:pic[p:nvPicPr/p:cNvPr[@id=$id]]",namespaces=NS,id=str(op.get("shape_id")))
            if len(pictures)!=1:raise FileEngineError("target_not_found","Picture shape ID does not exist")
            blip=pictures[0].find("p:blipFill/a:blip",NS)
            if blip is None:raise FileEngineError("unsupported_edit","Picture has no embedded image")
            data=image_path.read_bytes();digest=_digest(data);media_name=f"ppt/media/edited-{digest[:24]}.{extension}";parts[media_name]=data
            rel_name=str(PurePosixPath(name).parent/"_rels"/(PurePosixPath(name).name+".rels"));relations=_parse(parts[rel_name]);rid="rIdEdit"+digest[:24]
            existing=relations.xpath("./rel:Relationship[@Id=$id]",namespaces=NS,id=rid)
            if not existing:etree.SubElement(relations,"{"+NS["rel"]+"}Relationship",Id=rid,Type=NS["r"]+"/image",Target="../media/"+Path(media_name).name)
            blip.set("{"+NS["r"]+"}embed",rid);parts[rel_name]=_serialize(relations)
            types=_parse(parts["[Content_Types].xml"])
            if not any(node.get("Extension")==extension for node in types):
                etree.SubElement(types,"{http://schemas.openxmlformats.org/package/2006/content-types}Default",Extension=extension,ContentType="image/"+extension)
                parts["[Content_Types].xml"]=_serialize(types)
            changes.append({"slide_index":index,"shape_id":str(op["shape_id"]),"image_sha256":digest,"geometry_preserved":True})
            continue
        shapes=root.xpath(".//p:sp[p:nvSpPr/p:cNvPr[@id=$id]]",namespaces=NS,id=str(op.get("shape_id")))
        if len(shapes)!=1:raise FileEngineError("target_not_found","Text shape ID does not exist")
        nodes=shapes[0].xpath("./p:txBody/a:p/a:r/a:t",namespaces=NS)
        before="".join(n.text or "" for n in nodes)
        if op.get("type")=="replace_text":_replace(nodes,op.get("expected_text",""),op.get("text"),int(op.get("occurrences",1)))
        elif op.get("type")=="set_run_text":_text(_index(nodes,op.get("run_index"),"run_index"),op.get("text"))
        else:raise FileEngineError("unsupported_edit","PPTX supports replace_text and set_run_text on text shapes",status="unsupported")
        changes.append({"slide_index":index,"shape_id":str(op["shape_id"]),"before":before,"after":"".join(n.text or "" for n in nodes)})
    for name,root in loaded.items():parts[name]=_serialize(root)
    return changes


def _notebook(context: Context,operations: list[dict],candidate: Path) -> list[dict]:
    document=json.loads(context.source.read_text(encoding="utf-8"));changes=[]
    for op in operations:
        if op.get("type")!="set_cell_source":raise FileEngineError("unsupported_edit","Notebook editing supports set_cell_source",status="unsupported")
        cell=_index(document.get("cells",[]),op.get("cell_index"),"cell_index")
        if context.selector.get("cell_indices") is not None and op["cell_index"] not in context.selector["cell_indices"]:raise FileEngineError("selector_denied","Notebook cell is outside the authorized selection")
        before="".join(cell.get("source",[]))
        if "expected_text" in op and before!=op["expected_text"]:raise FileEngineError("edit_precondition_failed","Notebook cell changed")
        cell["source"]=str(op.get("text",""))
        if cell.get("cell_type")=="code":cell["outputs"]=[];cell["execution_count"]=None
        changes.append({"cell_index":op["cell_index"],"before":before,"after":cell["source"]})
    candidate.write_text(json.dumps(document,ensure_ascii=False,indent=1),encoding="utf-8")
    return changes


def process_edit(request: dict) -> dict:
    context=Context.from_request(request)
    operation=request.get("operation","")
    if operation=="notebook.execute":return _execute_notebook(context)
    if operation=="document.edit_verify":return _verify_candidate(context)
    if operation not in {"document.edit_prepare","document.edit_apply","notebook.edit"}:
        raise FileEngineError("unsupported_edit_operation","Unsupported edit operation",status="unsupported")
    suffix=context.source.suffix.lower()
    if suffix not in MIME:raise FileEngineError("unsupported_edit","Editing supports DOCX, XLSX, PPTX and IPYNB",status="unsupported")
    original=context.source.read_bytes();base=_digest(original)
    operations=context.options.get("operations",[])
    if not isinstance(operations,list) or not operations or len(operations)>1000:raise FileEngineError("invalid_edit","Provide 1–1000 typed edit operations")
    criteria={"fidelity":context.options.get("fidelity","L1"),"operations":operations,"selector":context.selector,"visual_regions":context.options.get("visual_regions",[])}
    criteria_digest=_digest(criteria)
    if criteria["fidelity"] not in {"L0","L1","L2"}:raise FileEngineError("invalid_fidelity","Use L0, L1 or L2")
    changeset={"base_sha256":base,"criteria_digest":criteria_digest,"operations":operations,"required_fidelity":criteria["fidelity"]}
    if operation=="document.edit_prepare":
        if suffix!=".ipynb":
            parts,_=_package(context.source)
            if suffix==".pptx":_pptx(parts,operations,context.selector,context)
            else:{".docx":_docx,".xlsx":_xlsx}[suffix](parts,operations,context.selector)
        result=context.response();result.update(changeset=changeset,required_fidelity=criteria["fidelity"]);return result
    if context.options.get("base_sha256")!=base:raise FileEngineError("base_version_conflict","The edit must identify the exact base SHA-256 from edit_prepare")
    candidate=context.output("candidate-"+criteria_digest[:16]+suffix)
    if candidate.resolve()==context.source.resolve():raise FileEngineError("overwrite_original","Candidate must not replace its source")
    if candidate.exists():raise FileEngineError("artifact_conflict","Use an isolated operation output_dir for each candidate")
    removed,changed,unchanged=[],[],0
    if suffix==".ipynb":changes=_notebook(context,operations,candidate)
    else:
        original_parts,infos=_package(context.source);parts=copy.copy(original_parts)
        changes=_pptx(parts,operations,context.selector,context) if suffix==".pptx" else {".docx":_docx,".xlsx":_xlsx}[suffix](parts,operations,context.selector)
        changed=[name for name,raw in parts.items() if original_parts.get(name)!=raw]
        removed=[name for name in original_parts if name not in parts]
        with zipfile.ZipFile(candidate,"w",compression=zipfile.ZIP_DEFLATED) as archive:
            for info in infos:
                if info.filename in parts:archive.writestr(info,parts[info.filename])
            for name in sorted(set(parts)-set(original_parts)):archive.writestr(name,parts[name])
        check,_=_package(candidate)
        if check!=parts:raise FileEngineError("verification_failed","Saved Office package differs from candidate bytes")
        unchanged=sum(1 for name,raw in original_parts.items() if check.get(name)==raw)
    if _digest(context.source.read_bytes())!=base:raise FileEngineError("source_modified","Original changed during editing")
    warnings=[];recalculation=None
    if suffix==".xlsx":
        warnings.append({"code":"not_recalculated","message":"Formula caches were invalidated; use explicit UNO recalculation before reporting evaluated values"})
        if context.options.get("recalculate"):
            from .recalculate import recalculate_workbook
            recalculated=context.output("recalculated-"+criteria_digest[:16]+suffix)
            recalculation=recalculate_workbook(candidate,recalculated,context.options)
            if recalculation["status"]!="verified":raise FileEngineError("formula_verification_failed","UNO recalculation produced missing/error caches or unexpected values")
            candidate=recalculated;warnings=[]
    fidelity="L1"
    requested=criteria["fidelity"]
    if requested not in {"L0","L1","L2"}:raise FileEngineError("invalid_fidelity","Use L0, L1 or L2")
    if requested=="L2":warnings.append({"code":"visual_verification_required","message":"Package preservation passed; L2 requires separate rendered-page verification"})
    verification={"passed":requested!="L2","status":"needs_visual_verification" if requested=="L2" else "verified", "base_sha256":base,"candidate_sha256":_digest(candidate.read_bytes()),"criteria_digest":criteria_digest,"fidelity":fidelity,
                  "changed_members":changed,"removed_members":removed,"unchanged_members":unchanged,"original_unchanged":True,"target_changes":changes,"render_verified":False,"recalculation":recalculation}
    report=context.output("verification-"+criteria_digest[:16]+".json");report.write_text(json.dumps(verification,ensure_ascii=False,indent=2,default=str))
    result=context.response(status="partial" if requested=="L2" else "ready",warnings=warnings,artifacts=[context.artifact(candidate,MIME[suffix]),context.artifact(report,"application/json")])
    result.update(changeset=changeset,verification=verification,required_fidelity=requested,
                  candidate={"path":str(candidate),"sha256":verification["candidate_sha256"],"size_bytes":candidate.stat().st_size,"mime_type":MIME[suffix]})
    return result


def _execute_notebook(context: Context) -> dict:
    if context.source.suffix.lower()!=".ipynb" or not context.options.get("execute_authorized"):
        raise FileEngineError("execution_not_authorized","Notebook execution requires an explicitly authorized sandbox operation")
    # This entrypoint is invoked only inside the isolated no-network processor
    # sandbox. It is not a Python sandbox for arbitrary code on the API host.
    import nbformat
    from nbclient import NotebookClient
    original=nbformat.read(context.source,as_version=4)
    selected=context.selector.get("cell_indices")
    if selected is None:selected=list(range(len(original.cells)))
    if not isinstance(selected,list) or not selected or any(isinstance(i,bool) or not isinstance(i,int) or i<0 or i>=len(original.cells) for i in selected) or len(set(selected))!=len(selected):
        raise FileEngineError("invalid_selector","Notebook selection must contain distinct valid cell indices")
    notebook=copy.deepcopy(original)
    notebook.cells=[copy.deepcopy(original.cells[i]) for i in selected]
    def merge_executed_cells():
        merged=copy.deepcopy(original)
        for index,cell in zip(selected,notebook.cells):merged.cells[index]=copy.deepcopy(cell)
        return merged
    if len(notebook.cells)>100:raise FileEngineError("notebook_cell_limit","Execute at most 100 selected cells per operation")
    timeout=min(max(int(context.options.get("timeout_seconds",30)),1),120)
    deadline=time.monotonic()+timeout
    def before_cell(**_):
        if time.monotonic()>deadline:raise TimeoutError("Notebook total deadline exceeded")
    candidate=context.output("executed.ipynb")
    if candidate.exists():raise FileEngineError("artifact_conflict","Use an isolated output directory")
    try:
        NotebookClient(notebook,timeout=timeout,kernel_name="python3",resources={"metadata":{"path":str(context.output_dir)}},allow_errors=False,on_cell_start=before_cell).execute()
    except Exception as exc:
        nbformat.write(merge_executed_cells(),candidate)
        result=context.response(status="failed",warnings=[{"code":"notebook_execution_failed","message":str(exc)[:400]}],artifacts=[context.artifact(candidate,MIME[".ipynb"])])
        result["verification"]={"status":"failed","passed":False,"execution_authorized":True};return result
    nbformat.write(merge_executed_cells(),candidate)
    result=context.response(artifacts=[context.artifact(candidate,MIME[".ipynb"])])
    required=context.options.get("fidelity","L1")
    if required not in {"L0","L1"}:raise FileEngineError("unsupported_fidelity","Notebook execution supports L0/L1, not visual L2")
    verification={"status":"verified","passed":True,"executed_cells":len(notebook.cells),"execution_authorized":True,"base_sha256":context.source_sha256,"candidate_sha256":_digest(candidate.read_bytes()),"criteria_digest":_digest({"operation":"notebook_execute","selector":context.selector,"timeout_seconds":timeout,"fidelity":required})}
    result.update(verification=verification,required_fidelity=required,candidate={"path":str(candidate),"sha256":verification["candidate_sha256"],"size_bytes":candidate.stat().st_size,"mime_type":MIME[".ipynb"]})
    return result


def _visual_verify(context: Context, candidate: Path) -> dict:
    """Exact fixed-renderer comparison outside explicitly allowed edit regions."""
    from pypdf import PdfReader
    from PIL import Image, ImageChops, ImageDraw
    from .render import office_pdf, render_pdf_pages
    folders=[context.output("base-render"),context.output("candidate-render")]
    for folder in folders:folder.mkdir(parents=True,exist_ok=True)
    pdfs=[]
    for source,folder in zip((context.source,candidate),folders):
        child=Context(source,source.parent,folder,{}, {},"render")
        pdfs.append(office_pdf(child))
    counts=[len(PdfReader(path).pages) for path in pdfs]
    if counts[0]!=counts[1]:return {"passed":False,"reason":"page_count_changed","page_counts":counts}
    if counts[0]>10:return {"passed":False,"reason":"verification_page_limit","page_counts":counts,"max_pages":10}
    regions=context.options.get("visual_regions",[])
    for region in regions:
        box=region.get("bbox",[])
        if not isinstance(region.get("page_index"),int) or not 0<=region["page_index"]<counts[0] or len(box)!=4 or any(not isinstance(n,(int,float)) or not 0<=n<=1 for n in box) or box[0]>=box[2] or box[1]>=box[3]:
            raise FileEngineError("invalid_visual_selector","Visual regions require a page_index and normalized bbox")
    rendered=[]
    for pdf,folder in zip(pdfs,folders):
        rendered.append(render_pdf_pages(Context(pdf,folder,folder,{}, {},"render"),list(range(counts[0]))))
    pages=[]
    for index,(before_path,after_path) in enumerate(zip(*rendered)):
        with Image.open(before_path) as before,Image.open(after_path) as after:
            if before.size!=after.size:
                pages.append({"page_index":index,"passed":False,"reason":"page_size_changed"});continue
            difference=ImageChops.difference(before.convert("RGB"),after.convert("RGB"))
            mask=ImageDraw.Draw(difference)
            for region in regions:
                if region["page_index"]==index:
                    x1,y1,x2,y2=region["bbox"]
                    mask.rectangle((int(x1*before.width),int(y1*before.height),int(x2*before.width),int(y2*before.height)),fill=(0,0,0))
            pages.append({"page_index":index,"passed":difference.getbbox() is None,"outside_region_difference":difference.getbbox()})
    return {"passed":all(page["passed"] for page in pages),"pages":pages,"renderer":"LibreOffice + Poppler","allowed_regions":regions,"page_counts":counts}


def _verify_candidate(context: Context) -> dict:
    candidate=Path(str(context.options.get("candidate_path",""))).resolve(strict=True)
    if not candidate.is_relative_to(context.input_root) or candidate==context.source:
        raise FileEngineError("candidate_not_authorized","Mount the immutable candidate separately within input_root")
    if candidate.stat().st_size>50<<20 or candidate.suffix.lower()!=context.source.suffix.lower():
        raise FileEngineError("candidate_limit","Candidate type/size does not match the file contract")
    base=_digest(context.source.read_bytes())
    if context.options.get("base_sha256")!=base or context.options.get("candidate_sha256")!=_digest(candidate.read_bytes()):
        raise FileEngineError("verification_precondition_failed","Base and candidate digests must match before verification")
    required=context.options.get("fidelity","L1")
    operations=context.options.get("operations",[])
    if not isinstance(operations,list) or not operations:raise FileEngineError("invalid_edit","Verification requires the frozen operations")
    original, _=_package(context.source)
    expected=copy.copy(original)
    suffix=context.source.suffix.lower()
    if suffix==".pptx":changes=_pptx(expected,operations,context.selector,context)
    elif suffix in {".docx",".xlsx"}:changes={".docx":_docx,".xlsx":_xlsx}[suffix](expected,operations,context.selector)
    else:raise FileEngineError("unsupported_verify","Independent verification currently supports Office candidates",status="unsupported")
    actual,_=_package(candidate)
    package_passed=actual==expected
    visual=_visual_verify(context,candidate) if required=="L2" and package_passed else None
    passed=package_passed and (required in {"L0","L1"} or required=="L2" and bool(visual and visual["passed"]))
    criteria={"fidelity":required,"operations":operations,"selector":context.selector,"visual_regions":context.options.get("visual_regions",[])}
    verification={"status":"verified" if passed else "failed","passed":passed,"base_sha256":base,"candidate_sha256":_digest(candidate.read_bytes()),
                  "criteria_digest":_digest(criteria),"fidelity":required,"package_matches_operations":package_passed,"visual":visual,"target_changes":changes}
    report=context.write_bytes("verification.json",json.dumps(verification,ensure_ascii=False,indent=2,default=str).encode())
    result=context.response(status="ready" if passed else "partial",artifacts=[context.artifact(report,"application/json")])
    result.update(verification=verification,required_fidelity=required)
    return result
