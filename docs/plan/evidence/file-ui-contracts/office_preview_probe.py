import sys,json,hashlib,tempfile,os
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'services/py-worker/src'))
from agentruntime_worker.files.cli import process
from docx import Document
from pptx import Presentation
from pptx.util import Inches
root=Path(tempfile.mkdtemp(prefix='office-preview-ui-'));inp=root/'input';inp.mkdir()
doc=Document();doc.add_paragraph('Page one readable text');doc.add_page_break();doc.add_paragraph('Page two selected text');doc.save(inp/'document.docx')
prs=Presentation()
for i in range(12):
 slide=prs.slides.add_slide(prs.slide_layouts[6]);box=slide.shapes.add_textbox(Inches(1), Inches(1), Inches(6), Inches(2));box.text=f'Slide {i+1}'
prs.save(inp/'presentation.pptx')
checks=[]
for filename,selector,expected in [('document.docx',{'page_index':1},[1]),('presentation.pptx',{'slide_index':2},[2]),('presentation.pptx',{},list(range(10)))]:
 p=inp/filename;before=hashlib.sha256(p.read_bytes()).hexdigest()
 result=process({'operation':'render','path':str(p),'input_root':str(inp),'output_dir':str(root/f'output-{len(checks)}'),'selector':selector})
 assert result['status'] in ('ready','partial'),result
 images=[a for a in result['artifacts'] if a['mime_type']=='image/png'];pdfs=[a for a in result['artifacts'] if a['mime_type']=='application/pdf']
 assert len(pdfs)==1 and len(images)==len(expected),result
 assert [a['selector']['page_index'] for a in images]==expected
 if filename.endswith('.pptx'): assert [a['selector']['slide_index'] for a in images]==expected
 assert result['coverage']['processed_pages']==expected
 assert before==hashlib.sha256(p.read_bytes()).hexdigest()
 checks.append({'source':filename,'selector':selector,'pages':expected,'status':result['status'],'source_unchanged':True,'artifacts':len(result['artifacts'])})
for selector in [{'slides':list(range(11))},{'slide_index':12},{'page_index':1,'slide_index':2}]:
 result=process({'operation':'render','path':str(inp/'presentation.pptx'),'input_root':str(inp),'output_dir':str(root/f'reject-{len(checks)}'),'selector':selector})
 assert result['status']=='failed',result
 checks.append({'selector':selector,'rejected':result['error']['code']})
out={'mode':'real LibreOffice + Poppler; generated DOCX/PPTX fixture','checks':checks}
Path(os.environ.get('FILE_UI_OFFICE_RESULT','/tmp/file-office-preview-results.json')).write_text(json.dumps(out,indent=2)+'\n');print(json.dumps(out,indent=2))
