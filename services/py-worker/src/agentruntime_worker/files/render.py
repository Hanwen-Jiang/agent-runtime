"""Page rendering, bounded crops and real offline RapidOCR (ONNX Runtime CPU)."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

from .models import Context, FileEngineError, indices, MAX_PAGES
from .inspect import capabilities, media_type

MAX_IMAGE_PIXELS = 20_000_000


def rapidocr_paths() -> dict[str, Path]:
    root = os.environ.get("FILE_ENGINES_MODEL_DIR", "")
    if not root:
        raise FileEngineError("ocr_models_missing", "offline OCR model directory is not configured", "unsupported")
    paths = {kind: Path(root) / "rapidocr" / f"{kind}.onnx" for kind in ("det", "cls", "rec")}
    if any(not path.is_file() for path in paths.values()):
        raise FileEngineError("ocr_models_missing", "offline RapidOCR detection/classification/recognition models are missing", "unsupported")
    return paths


@lru_cache(maxsize=1)
def ocr_engine():
    from rapidocr import RapidOCR
    paths = rapidocr_paths()
    return RapidOCR(params={"Det.model_path": str(paths["det"]), "Cls.model_path": str(paths["cls"]), "Rec.model_path": str(paths["rec"]),
                            "EngineConfig.onnxruntime.intra_op_num_threads": 2, "EngineConfig.onnxruntime.inter_op_num_threads": 1})


def ocr_image(ctx: Context, path: Path, anchor: dict) -> list[dict]:
    from PIL import Image
    with Image.open(path) as image:
        width, height = image.size
        if width * height > MAX_IMAGE_PIXELS:
            raise FileEngineError("image_pixel_limit", "OCR image exceeds 20 million pixels")
    result = ocr_engine()(str(path))
    blocks = []
    texts = result.txts if result.txts is not None else []
    for index, text in enumerate(texts):
        points = result.boxes[index]
        x = [float(p[0]) / width for p in points]
        y = [float(p[1]) / height for p in points]
        region = {**anchor, "bbox": [min(x), min(y), max(x), max(y)], "coordinate_space": "normalized_top_left_after_rotation"}
        score = float(result.scores[index])
        blocks.append(ctx.block("ocr_text", text, region, derivation="ocr", confidence=score, low_confidence=score < .8))
    return blocks


def checked_bbox(value):
    if not isinstance(value, list) or len(value) != 4 or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not 0 <= x <= 1 for x in value) or value[0] >= value[2] or value[1] >= value[3]:
        raise FileEngineError("invalid_selector", "bbox must be normalized [left,top,right,bottom]")
    return value


def render_pdf_pages(ctx: Context, pages: list[int]) -> list[Path]:
    executable = shutil.which("pdftoppm")
    if not executable:
        raise FileEngineError("pdf_renderer_missing", "Poppler pdftoppm is not installed", "unsupported")
    if len(pages) > MAX_PAGES:
        raise FileEngineError("selector_limit", "render accepts at most 10 pages")
    paths = []
    for index in pages:
        target = ctx.output(f"page-{index:05d}.png")
        if target.exists():
            raise FileEngineError("artifact_conflict", "page artifact already exists; use an isolated operation output_dir")
        args = [executable, "-f", str(index+1), "-l", str(index+1), "-r", "144", "-scale-to", "4096", "-png", "-singlefile", str(ctx.source), str(target.with_suffix(""))]
        result = subprocess.run(args, capture_output=True, timeout=60, check=False)
        if result.returncode or not target.is_file():
            raise FileEngineError("pdf_render_failed", "Poppler failed to render the selected page")
        from PIL import Image
        with Image.open(target) as image:
            if image.width * image.height > MAX_IMAGE_PIXELS:
                target.unlink()
                raise FileEngineError("image_pixel_limit", "rendered page exceeds pixel limit")
        if "bbox" in ctx.selector:
            box = checked_bbox(ctx.selector["bbox"])
            with Image.open(target) as full:
                crop = full.crop((int(box[0]*full.width),int(box[1]*full.height),int(box[2]*full.width),int(box[3]*full.height)))
                if crop.width < 1 or crop.height < 1:
                    raise FileEngineError("invalid_selector", "crop is smaller than a pixel")
                crop.save(target, "PNG")
        paths.append(target)
    return paths


def image_read(ctx: Context) -> dict:
    from PIL import Image, ImageOps
    with Image.open(ctx.source) as original:
        if original.width * original.height > MAX_IMAGE_PIXELS:
            raise FileEngineError("image_pixel_limit", "image exceeds 20 million decoded pixels")
        image = ImageOps.exif_transpose(original)
        anchor = {"kind": "image_region", "bbox": [0, 0, 1, 1], "coordinate_space": "normalized_top_left_after_rotation"}
        cropped = False
        if "bbox" in ctx.selector:
            box = checked_bbox(ctx.selector["bbox"])
            anchor["bbox"] = box
            pixel = (int(box[0]*image.width), int(box[1]*image.height), max(int(box[2]*image.width),1), max(int(box[3]*image.height),1))
            image = image.crop(pixel)
            cropped = True
        image.thumbnail((4096,4096))
        path = ctx.output("image-region.png")
        if path.exists():
            raise FileEngineError("artifact_conflict", "image artifact already exists")
        image.convert("RGB" if image.mode not in ("RGB","RGBA") else image.mode).save(path,"PNG")
    blocks = []
    if ctx.options.get("ocr", False):
        blocks = ocr_image(ctx, path, anchor)
        if cropped:
            left,top,right,bottom=anchor["bbox"]
            for block in blocks:
                a,b,c,d=block["anchor"]["bbox"]
                block["anchor"]["bbox"]=[left+a*(right-left),top+b*(bottom-top),left+c*(right-left),top+d*(bottom-top)]
    return ctx.response(blocks=blocks, artifacts=[ctx.artifact(path,"image/png",anchor)], capabilities=capabilities(),
                        coverage={"selection":anchor,"visual_content_in_artifact":True,"ocr_requested":bool(ctx.options.get("ocr")),"ocr_regions":len(blocks)})


def office_pdf(ctx: Context) -> Path:
    executable = shutil.which("soffice")
    if not executable:
        raise FileEngineError("office_renderer_missing", "LibreOffice is not installed", "unsupported")
    with tempfile.TemporaryDirectory(prefix="file-render-profile-") as profile:
        user=Path(profile)/"user";user.mkdir()
        (user/"registrymodifications.xcu").write_text('''<?xml version="1.0"?><oor:items xmlns:oor="http://openoffice.org/2001/registry"><item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item><item oor:path="/org.openoffice.Office.Calc/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>2</value></prop></item></oor:items>''')
        result=subprocess.run([executable,"-env:UserInstallation="+Path(profile).as_uri(),"--headless","--nologo","--nodefault","--nolockcheck","--norestore","--convert-to","pdf","--outdir",str(ctx.output_dir),str(ctx.source)],capture_output=True,timeout=120,check=False)
    path=ctx.output_dir/(ctx.source.stem+'.pdf')
    if result.returncode or not path.is_file():
        raise FileEngineError("office_render_failed", "LibreOffice did not produce a PDF")
    return path


def render(ctx: Context) -> dict:
    mime=media_type(ctx.source)
    if mime.startswith('image/'):
        return image_read(ctx)
    if mime=='application/pdf':
        from pypdf import PdfReader
        reader=PdfReader(ctx.source)
        if reader.is_encrypted:raise FileEngineError('encrypted_document','encrypted PDF cannot be rendered')
        pages,partial=indices(ctx.selector,len(reader.pages))
        paths=render_pdf_pages(ctx,pages)
        artifacts=[ctx.artifact(path,'image/png',{'kind':'pdf_region','page_index':index,'bbox':ctx.selector.get('bbox',[0,0,1,1])}) for path,index in zip(paths,pages)]
        return ctx.response(status='partial' if partial else 'ready',artifacts=artifacts,coverage={'total_pages':len(reader.pages),'processed_pages':pages},capabilities=capabilities())
    if ctx.source.suffix.lower() in ('.docx','.xlsx','.pptx','.odt','.ods','.odp','.rtf','.doc','.xls','.ppt'):
        path=office_pdf(ctx)
        return ctx.response(artifacts=[ctx.artifact(path,'application/pdf',{'kind':'rendered_document','renderer':'libreoffice'})],coverage={'rendered':True,'text_layout_preservation':'requires_visual_review'},capabilities=capabilities())
    raise FileEngineError('unsupported_render','no renderer for this format','unsupported')
