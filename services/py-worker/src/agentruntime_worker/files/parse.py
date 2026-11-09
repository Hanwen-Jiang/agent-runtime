"""Structural readers and bounded archive handling. Original bytes are immutable."""
from __future__ import annotations

import csv
import io
import json
import os
import stat
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from .models import Context, FileEngineError, MAX_ARCHIVE_MEMBERS, MAX_EXPANDED_BYTES, MAX_CELLS, indices, positive, canonical
from .inspect import capabilities, media_type


def safe_member(name: str) -> str:
    pure = PurePosixPath(name)
    if not name or "\\" in name or "\x00" in name or pure.is_absolute() or ".." in pure.parts or ":" in pure.parts[0] or len(pure.parts) > 20:
        raise FileEngineError("unsafe_archive_member", "archive contains an unsafe member path")
    return str(pure)


def check_zip(path: Path) -> list:
    with zipfile.ZipFile(path) as archive:
        items = archive.infolist()
        if len(items) > MAX_ARCHIVE_MEMBERS or sum(item.file_size for item in items) > MAX_EXPANDED_BYTES:
            raise FileEngineError("archive_limit", "archive exceeds 1000 members / 200 MiB expanded bytes")
        seen = set()
        for item in items:
            name = safe_member(item.filename)
            if name in seen:
                raise FileEngineError("duplicate_archive_member", "archive contains duplicate member names")
            seen.add(name)
            mode = item.external_attr >> 16
            if stat.S_ISLNK(mode) or item.flag_bits & 1:
                raise FileEngineError("unsafe_archive_member", "symlinks and encrypted ZIP members are not supported")
            if item.file_size > 100 * max(item.compress_size, 1024):
                raise FileEngineError("archive_ratio_limit", "archive member expansion ratio exceeds 100")
        return items


def archive(ctx: Context) -> dict:
    extract = ctx.operation in ("extract", "archive.extract")
    requested = ctx.selector.get("members")
    if extract and (not isinstance(requested, list) or not requested or len(requested) > 100):
        raise FileEngineError("invalid_selector", "extract requires 1..100 explicit member paths")
    requested = set(requested or [])
    items, artifacts, blocks, seen = [], [], [], set()
    if zipfile.is_zipfile(ctx.source):
        check_zip(ctx.source)
        reader = zipfile.ZipFile(ctx.source)
        entries = [(i.filename, i.file_size, i.is_dir(), i) for i in reader.infolist()]
        def read_member(item):
            return reader.open(item)
    else:
        try:
            reader = tarfile.open(ctx.source, "r:*")
        except tarfile.TarError:
            raise FileEngineError("unsupported_archive", "supported archives are ZIP and TAR", "unsupported") from None
        members = reader.getmembers()
        if len(members) > MAX_ARCHIVE_MEMBERS or sum(m.size for m in members) > MAX_EXPANDED_BYTES:
            reader.close()
            raise FileEngineError("archive_limit", "archive exceeds expansion limits")
        entries = [(m.name, m.size, m.isdir(), m) for m in members]
        def read_member(item):
            return reader.extractfile(item)
    try:
        total = 0
        for name, size, directory, item in entries:
            safe = safe_member(name)
            if safe in seen:
                raise FileEngineError("duplicate_archive_member", "duplicate archive member")
            seen.add(safe)
            if isinstance(item, tarfile.TarInfo) and not (item.isfile() or item.isdir()):
                raise FileEngineError("unsafe_archive_member", "archive links/devices are not accepted")
            total += size
            if total > MAX_EXPANDED_BYTES:
                raise FileEngineError("archive_limit", "expanded bytes exceed limit")
            items.append({"name": name, "size_bytes": size, "directory": directory})
        if requested - seen:
            raise FileEngineError("member_not_found", "one or more selected archive members do not exist")
        for name, size, directory, item in entries:
            anchor = {"kind": "archive_member", "member": name}
            blocks.append(ctx.block("archive_member", name, anchor, size_bytes=size, directory=directory))
            if extract and name in requested and not directory:
                with read_member(item) as stream:
                    data = stream.read(MAX_EXPANDED_BYTES + 1)
                if len(data) != size or len(data) > MAX_EXPANDED_BYTES:
                    raise FileEngineError("archive_size_mismatch", "archive member differs from its declared size")
                dest = ctx.write_bytes("members/" + safe_member(name), data)
                artifacts.append(ctx.artifact(dest, media_type(dest), anchor))
    finally:
        reader.close()
    return ctx.response(blocks=blocks, artifacts=artifacts, outline=items, capabilities=capabilities(),
                        coverage={"total_members": len(entries), "extracted_members": len(artifacts), "selected_members": sorted(requested)})


def text_read(ctx: Context) -> dict:
    encoding = ctx.options.get("encoding", "utf-8-sig")
    if encoding not in ("utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "gb18030", "latin-1"):
        raise FileEngineError("unsupported_encoding", "encoding must be explicitly supported", "unsupported")
    try:
        text = ctx.source.read_bytes().decode(encoding)
    except UnicodeError:
        raise FileEngineError("encoding_required", "file is not valid in the selected encoding; specify encoding") from None
    if "\x00" in text:
        raise FileEngineError("binary_file", "binary content is not a text document", "unsupported")
    lines = text.splitlines()
    start = positive(ctx.selector.get("start_line"), 1, max(len(lines), 1), "start_line")
    count = positive(ctx.selector.get("max_lines"), 200, 2000, "max_lines")
    picked = lines[start-1:start-1+count]
    blocks = [ctx.block("text", "\n".join(picked), {"kind": "text_range", "start_line": start, "end_line": start+len(picked)-1, "offset_unit": "unicode_code_point"})]
    warnings = []
    if ctx.source.suffix.lower() in (".json", ".jsonl", ".yaml", ".yml", ".xml"):
        try:
            if ctx.source.suffix.lower() == ".json":
                parsed = json.loads(text)
            elif ctx.source.suffix.lower() == ".jsonl":
                parsed = [json.loads(line) for line in picked if line.strip()]
            elif ctx.source.suffix.lower() in (".yaml", ".yml"):
                import yaml
                parsed = yaml.safe_load(text)
            else:
                from defusedxml.ElementTree import fromstring
                parsed = fromstring(text).tag
            outline = [{"kind": "structure", "type": type(parsed).__name__, "keys": list(parsed)[:100] if isinstance(parsed, dict) else [], "count": len(parsed) if isinstance(parsed, (list, dict)) else None}]
        except Exception as error:
            warnings.append({"code": "invalid_structure", "message": type(error).__name__})
            outline = []
    else:
        outline = [{"kind": "heading", "line": i+1, "text": line[:200]} for i, line in enumerate(lines) if line.startswith("#")][:100]
    partial = start != 1 or len(picked) < len(lines)
    return ctx.response(status="partial" if partial else "ready", blocks=blocks, outline=outline, warnings=warnings,
                        coverage={"total_lines": len(lines), "start_line": start, "end_line": start+len(picked)-1, "next_line": start+len(picked) if start-1+len(picked)<len(lines) else None})


def pdf_read(ctx: Context) -> dict:
    if ctx.options.get("layout") == "complex" and "bbox" not in ctx.selector:
        return docling_read(ctx)
    from pypdf import PdfReader
    reader = PdfReader(ctx.source)
    if reader.is_encrypted:
        raise FileEngineError("encrypted_document", "encrypted PDF cannot be read without an authorized decrypt operation")
    pages, partial = indices(ctx.selector, len(reader.pages))
    blocks, warnings, artifacts = [], [], []
    for index in pages:
        page = reader.pages[index]
        text = page.extract_text() or ""
        anchor = {"kind": "pdf_region", "page_index": index, "bbox": [0, 0, 1, 1], "coordinate_space": "normalized_top_left_after_rotation"}
        if ctx.selector.get("bbox") is not None:
            from .render import render_pdf_pages, ocr_image
            paths = render_pdf_pages(ctx, [index])
            anchor["bbox"] = ctx.selector["bbox"]
            artifacts.extend(ctx.artifact(path, "image/png", anchor) for path in paths)
            selected_blocks = ocr_image(ctx, paths[0], anchor)
            left, top, right, bottom = anchor["bbox"]
            for block in selected_blocks:
                a,b,c,d = block["anchor"]["bbox"]
                block["anchor"]["bbox"] = [left+a*(right-left),top+b*(bottom-top),left+c*(right-left),top+d*(bottom-top)]
            blocks.extend(selected_blocks)
            if not selected_blocks:
                warnings.append({"code":"ocr_empty","message":"OCR found no text in the selected region","page_index":index})
            partial = True
        elif text.strip():
            blocks.append(ctx.block("text", text, anchor, derivation="native_text"))
        elif ctx.options.get("ocr", True):
            from .render import render_pdf_pages, ocr_image
            paths = render_pdf_pages(ctx, [index])
            artifacts.extend(ctx.artifact(path, "image/png", anchor) for path in paths)
            try:
                recognized = ocr_image(ctx, paths[0], anchor)
                blocks.extend(recognized)
                if not recognized:
                    warnings.append({"code":"ocr_empty","message":"OCR found no text; page may require visual review","page_index":index})
                    partial = True
            except FileEngineError as error:
                warnings.append({"code": error.code, "message": error.message, "page_index": index})
                partial = True
        else:
            warnings.append({"code": "no_native_text", "message": "page has no native text; OCR was not requested", "page_index": index})
            partial = True
    return ctx.response(status="partial" if partial else "ready", blocks=blocks, warnings=warnings, artifacts=artifacts,
                        outline=[{"kind": "pdf", "pages": len(reader.pages)}], capabilities=capabilities(),
                        coverage={"total_pages": len(reader.pages), "processed_pages": pages, "unselected_pages": len(reader.pages)-len(pages)})


def docling_read(ctx: Context) -> dict:
    models = os.environ.get("FILE_ENGINES_MODEL_DIR", "")
    if not models or not (Path(models) / "docling").is_dir():
        raise FileEngineError("docling_models_missing", "offline Docling model bundle is not installed", "unsupported")
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(ctx.source)
    if reader.is_encrypted:
        raise FileEngineError("encrypted_document", "encrypted PDF is not supported")
    pages, partial = indices(ctx.selector, len(reader.pages))
    writer = PdfWriter()
    for index in pages:
        writer.add_page(reader.pages[index])
    buf = io.BytesIO();writer.write(buf)
    selected = ctx.write_bytes("docling-selected.pdf", buf.getvalue())
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, RapidOcrOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from .render import rapidocr_paths
    paths = rapidocr_paths()
    options = PdfPipelineOptions(artifacts_path=Path(models) / "docling", do_ocr=bool(ctx.options.get("ocr", True)), do_table_structure=True)
    options.ocr_options = RapidOcrOptions(backend="onnxruntime", force_full_page_ocr=True, scale=2.0, det_model_path=str(paths["det"]), rec_model_path=str(paths["rec"]), cls_model_path=str(paths["cls"]))
    converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
    converted = converter.convert(selected, max_num_pages=10).document
    blocks = []
    for item, level in converted.iterate_items():
        for prov in getattr(item, "prov", []):
            local = prov.page_no - 1
            if not 0 <= local < len(pages):
                continue
            page = converted.pages[prov.page_no]
            box = prov.bbox.to_top_left_origin(page_height=page.size.height)
            bbox = [box.l/page.size.width, box.t/page.size.height, box.r/page.size.width, box.b/page.size.height]
            text = getattr(item, "text", "")
            if hasattr(item, "export_to_markdown") and not text:
                text = item.export_to_markdown(doc=converted)
            blocks.append(ctx.block(str(item.label), text, {"kind": "pdf_region", "page_index": pages[local], "bbox": bbox, "coordinate_space": "normalized_top_left_after_rotation"}, derivation="docling", level=level))
    warnings = []
    if not any(block["text"].strip() for block in blocks):
        partial = True
        warnings.append({"code": "layout_empty", "message": "layout engine returned no text; use basic PDF OCR or rendered-page review"})
    ir = ctx.write_bytes("docling-ir.json", canonical(converted.export_to_dict()))
    return ctx.response(status="partial" if partial else "ready", blocks=blocks, warnings=warnings, artifacts=[ctx.artifact(ir, "application/json")], capabilities=capabilities(),
                        coverage={"total_pages": len(reader.pages), "processed_pages": pages, "layout_backend": "docling"})


def docx_read(ctx: Context) -> dict:
    check_zip(ctx.source)
    from docx import Document
    doc = Document(ctx.source)
    blocks, outline = [], []
    for index, item in enumerate(doc.iter_inner_content()):
        anchor = {"kind": "document_block", "block_index": index}
        if hasattr(item, "rows"):
            text = "\n".join("\t".join(c.text for c in row.cells) for row in item.rows)
            blocks.append(ctx.block("table", text, anchor, rows=len(item.rows)))
        else:
            blocks.append(ctx.block("paragraph", item.text, anchor, style=item.style.name))
            if item.style.name.startswith("Heading"):
                outline.append({"block_index": index, "text": item.text, "style": item.style.name})
    for sec_id, section in enumerate(doc.sections):
        for kind in ("header", "footer"):
            content = getattr(section, kind)
            for index, paragraph in enumerate(content.paragraphs):
                if paragraph.text:
                    blocks.append(ctx.block(kind, paragraph.text, {"kind": "document_block", "section": sec_id, "part": kind, "block_index": index}))
    start = ctx.selector.get("block_index", ctx.selector.get("paragraph_index", ctx.selector.get("start", 0)))
    count = 1 if "block_index" in ctx.selector or "paragraph_index" in ctx.selector else positive(ctx.selector.get("count"), 200, 500, "count")
    if not isinstance(start, int) or isinstance(start, bool) or start < 0 or start > len(blocks):
        raise FileEngineError("invalid_selector", "block start is outside document")
    selected = blocks[start:start+count]
    xml = doc.part.blob
    warnings = [{"code": "revisions_present", "message": "tracked revisions exist; extracted text is not an accepted/rejected revision view"}] if b"<w:ins" in xml or b"<w:del" in xml else []
    return ctx.response(status="partial" if len(selected)<len(blocks) else "ready", blocks=selected, outline=outline, warnings=warnings,
                        coverage={"total_blocks": len(blocks), "start": start, "returned": len(selected), "inline_images": len(doc.inline_shapes), "images_read": False}, capabilities=capabilities())


def xlsx_read(ctx: Context) -> dict:
    check_zip(ctx.source)
    from openpyxl import load_workbook
    from openpyxl.utils.cell import range_boundaries
    wb = load_workbook(ctx.source, data_only=False, read_only=True, keep_links=False)
    cached = load_workbook(ctx.source, data_only=True, read_only=True, keep_links=False)
    try:
        name = ctx.selector.get("sheet")
        if name is None and "sheet_id" in ctx.selector:
            sheet_id = str(ctx.selector["sheet_id"])
            if not sheet_id.startswith("sheet-") or not sheet_id[6:].isdigit() or int(sheet_id[6:]) >= len(wb.sheetnames):
                raise FileEngineError("sheet_not_found", "invalid worksheet identity")
            name = wb.sheetnames[int(sheet_id[6:])]
        name = name or wb.sheetnames[0]
        if name not in wb.sheetnames:
            raise FileEngineError("sheet_not_found", "selected worksheet does not exist")
        ws, values = wb[name], cached[name]
        selected_range = ctx.selector.get("range", f"A1:{min(max(ws.max_column or 1,1),20)}")
        if "range" in ctx.selector:
            a,b,c,d = range_boundaries(selected_range)
            if None in (a,b,c,d) or a<1 or b<1 or c<a or d<b:
                raise FileEngineError("invalid_selector", "range must be a bounded A1 rectangle")
        else:
            a,b,c,d = 1,1,min(ws.max_column or 1,20),min(ws.max_row or 1,50)
        if (c-a+1)*(d-b+1)>MAX_CELLS:
            raise FileEngineError("selector_limit", "worksheet read exceeds 2000 cells")
        blocks=[]
        for row, cached_row in zip(ws.iter_rows(min_row=b,max_row=d,min_col=a,max_col=c), values.iter_rows(min_row=b,max_row=d,min_col=a,max_col=c)):
            for cell, old in zip(row,cached_row):
                if cell.value is None:
                    continue
                formula=cell.value if cell.data_type=='f' else None
                blocks.append(ctx.block("cell", str(cell.value), {"kind":"sheet_range","sheet_id":f"sheet-{wb.sheetnames.index(name)}","sheet":name,"range":cell.coordinate},
                                        value=None if formula else cell.value,formula=formula,cached_value=old.value,calculation_status="not_recalculated",number_format=cell.number_format))
        partial=len(wb.sheetnames)>1 or d<(ws.max_row or 0) or c<(ws.max_column or 0) or a>1 or b>1
        return ctx.response(status="partial" if partial else "ready",blocks=blocks,outline=[{"kind":"sheet","name":s.title,"sheet_id":f"sheet-{i}","rows":s.max_row,"columns":s.max_column,"state":s.sheet_state} for i,s in enumerate(wb) if not ctx.options.get('_restrict_sheet') or s.title==name],
                            coverage={"sheet":name,"range":[a,b,c,d],"formulas_recalculated":False},capabilities=capabilities())
    finally:
        wb.close();cached.close()


def pptx_read(ctx: Context) -> dict:
    check_zip(ctx.source)
    from pptx import Presentation
    prs=Presentation(ctx.source)
    slides,partial=indices(ctx.selector,len(prs.slides),singular="slide_index",plural="slides")
    blocks=[]
    for index in slides:
        slide=prs.slides[index]
        for shape in slide.shapes:
            if "shape_id" in ctx.selector and shape.shape_id != ctx.selector["shape_id"]:
                partial = True
                continue
            anchor={"kind":"slide_shape","slide_index":index,"shape_id":shape.shape_id,"bbox":[shape.left/prs.slide_width,shape.top/prs.slide_height,(shape.left+shape.width)/prs.slide_width,(shape.top+shape.height)/prs.slide_height]}
            if shape.has_text_frame:
                blocks.append(ctx.block("text",shape.text,anchor))
            if shape.has_table:
                blocks.append(ctx.block("table","\n".join("\t".join(c.text for c in row.cells) for row in shape.table.rows),anchor))
            if shape.shape_type==13:
                blocks.append(ctx.block("image","",anchor,mime_type=shape.image.content_type,sha256=__import__('hashlib').sha256(shape.image.blob).hexdigest(),visual_read=False))
        if slide.has_notes_slide and "shape_id" not in ctx.selector:
            blocks.append(ctx.block("notes",slide.notes_slide.notes_text_frame.text,{"kind":"slide_shape","slide_index":index,"part":"notes"}))
    return ctx.response(status="partial" if partial else "ready",blocks=blocks,outline=[{"kind":"slide","slide_index":i,"title":s.shapes.title.text if s.shapes.title else ""} for i,s in enumerate(prs.slides)],coverage={"total_slides":len(prs.slides),"processed_slides":slides,"charts_visual_only":True},capabilities=capabilities())


def notebook_read(ctx: Context) -> dict:
    data=json.loads(ctx.source.read_text('utf-8'))
    if data.get('nbformat')!=4 or not isinstance(data.get('cells'),list):
        raise FileEngineError('invalid_notebook','expected nbformat 4 cells')
    selected,partial=indices(ctx.selector,len(data['cells']),singular='cell_index',plural='cells',maximum=100)
    blocks=[];artifacts=[]
    for index in selected:
        cell=data['cells'][index];text=cell.get('source',[]);text=''.join(text) if isinstance(text,list) else str(text)
        anchor={'kind':'notebook_cell','cell_index':index,'cell_id':cell.get('id',str(index))}
        blocks.append(ctx.block(cell.get('cell_type','unknown'),text,anchor,executed=False))
        for output in cell.get('outputs',[])[:20]:
            values=output.get('data',{});plain=values.get('text/plain',output.get('text',''));plain=''.join(plain) if isinstance(plain,list) else str(plain)
            if plain: blocks.append(ctx.block('stored_output',plain,{**anchor,'output_index':len(blocks)},executed=False))
            if 'image/png' in values:
                import base64
                encoded=values['image/png'];encoded=''.join(encoded) if isinstance(encoded,list) else encoded
                if len(encoded)>14<<20:raise FileEngineError('output_limit','stored notebook image too large')
                image=base64.b64decode(encoded,validate=True)
                dest=ctx.write_bytes(f'notebook-{index}-{len(artifacts)}.png',image)
                artifacts.append(ctx.artifact(dest,'image/png',anchor))
    return ctx.response(status='partial' if partial else 'ready',blocks=blocks,artifacts=artifacts,coverage={'total_cells':len(data['cells']),'processed_cells':selected,'executed':False},capabilities=capabilities())


def csv_read(ctx: Context) -> dict:
    start=ctx.selector.get('start',0);count=positive(ctx.selector.get('count'),50,500,'count')
    if not isinstance(start,int) or isinstance(start,bool) or start<0:raise FileEngineError('invalid_selector','invalid row start')
    delimiter='\t' if ctx.source.suffix.lower()=='.tsv' else ','
    rows=[];total=0
    with ctx.source.open(encoding=ctx.options.get('encoding','utf-8-sig'),newline='') as stream:
        reader=csv.reader(stream,delimiter=delimiter);headers=next(reader,[])
        for i,row in enumerate(reader):
            total+=1
            if start<=i<start+count:rows.append(ctx.block('row','\t'.join(row),{'kind':'sheet_range','sheet_id':'csv','row_index':i},values=row))
    return ctx.response(status='partial' if len(rows)<total else 'ready',blocks=rows,outline=[{'kind':'table','columns':headers,'rows':total}],coverage={'total_rows':total,'start':start,'returned':len(rows)},capabilities=capabilities())


def read(ctx: Context) -> dict:
    suffix=ctx.source.suffix.lower();mime=media_type(ctx.source)
    if mime=='application/pdf':return pdf_read(ctx)
    if suffix=='.docx':return docx_read(ctx)
    if suffix=='.xlsx':return xlsx_read(ctx)
    if suffix=='.pptx':return pptx_read(ctx)
    if suffix=='.ipynb':return notebook_read(ctx)
    if suffix in ('.csv','.tsv'):return csv_read(ctx)
    if mime.startswith('image/'):
        from .render import image_read
        return image_read(ctx)
    if suffix=='.parquet':
        from .compute import process_compute
        return process_compute({'operation':'table.sample','path':str(ctx.source),'input_root':str(ctx.input_root),'output_dir':str(ctx.output_dir),'selector':ctx.selector,'options':ctx.options})
    if suffix in ('.zip','.tar','.tgz','.gz'):return archive(ctx)
    if mime.startswith('text/') or suffix in ('.json','.jsonl','.xml','.yaml','.yml','.log','.py','.js','.ts','.go','.rs','.sh','.css','.sql'):
        return text_read(ctx)
    raise FileEngineError('unsupported_format','no structural reader installed for this format','unsupported')
