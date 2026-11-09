import json,hashlib,sys,tempfile
from pathlib import Path
from docx import Document
from pptx import Presentation
from openpyxl import Workbook,load_workbook
import nbformat
from agentruntime_worker.files.compute import process_compute
from agentruntime_worker.files.edit import process_edit
from agentruntime_worker.files.models import FileEngineError
root=Path(tempfile.mkdtemp(prefix='file-m3-'));source=root/'inputs';source.mkdir();results={}
def req(path,op,options=None,selector=None):
 out=root/(str(len(list(root.iterdir())))+'-out')
 return dict(operation=op,path=str(path),input_root=str(source),output_dir=str(out),options=options or {},selector=selector or {})
p=source/'numbers.csv';p.write_text('name,value,private\na,2,secret\nb,3,hidden\n')
r=process_compute(req(p,'table.query',{'sql':'SELECT sum(value) AS total FROM data'},{'columns':['value']}));assert r['rows']==[[5]],r;results['csv_sum']=r['rows']
for sql in ["COPY data TO '/tmp/escape.csv'","SELECT * FROM read_csv_auto('/etc/passwd')","INSTALL httpfs","ATTACH '/tmp/any.db' AS x","SELECT * FROM data;SELECT 2"]:
 try:process_compute(req(p,'table.query',{'sql':sql}));raise AssertionError('unsafe query accepted')
 except FileEngineError:pass
try:process_compute(req(p,'table.query',{'sql':'SELECT private FROM data'},{'columns':['value']}));raise AssertionError('unselected column accepted')
except FileEngineError:pass
results['sql_boundaries']='passed'
book=Workbook();book.active.title='Data';book.active.append(['n','v']);book.active.append(['a',2]);book.active.append(['b',3]);book.create_sheet('Private')['A1']='keep';p=source/'book.xlsx';book.save(p)
r=process_compute(req(p,'table.query',{'sql':'SELECT sum(v) FROM data'},{'sheet':'Data','range':'A1:B3'}));assert r['rows']==[[5]],r
options={'operations':[{'type':'set_cell','sheet':'Data','cell':'C5','value':42},{'type':'set_formula','sheet':'Data','cell':'B4','formula':'=SUM(B2:B3)'}],'base_sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
r=process_edit(req(p,'document.edit_apply',options,{'sheet':'Data'}));candidate=Path(r['artifacts'][0]['path']);got=load_workbook(candidate);assert got['Private']['A1'].value=='keep';assert got['Data']['C5'].value==42;assert got['Data']['B4'].value=='=SUM(B2:B3)';results['xlsx_edit']=r['verification']['status']
doc=Document();para=doc.add_paragraph();para.add_run('Hello ').bold=True;para.add_run('world');doc.add_paragraph('Keep me');p=source/'doc.docx';doc.save(p)
r=process_edit(req(p,'document.edit_apply',{'base_sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'operations':[{'type':'replace_text','paragraph_index':0,'expected_text':'Hello world','text':'Updated text'}]}));got=Document(r['artifacts'][0]['path']);assert got.paragraphs[0].text=='Updated text';assert got.paragraphs[1].text=='Keep me';results['docx_edit']=r['verification']['status']
slides=Presentation();slide=slides.slides.add_slide(slides.slide_layouts[0]);slide.shapes.title.text='Hello slide';p=source/'slides.pptx';slides.save(p)
r=process_edit(req(p,'document.edit_apply',{'base_sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'operations':[{'type':'replace_text','slide_index':0,'shape_id':slide.shapes.title.shape_id,'expected_text':'Hello slide','text':'New slide'}]}));assert Presentation(r['artifacts'][0]['path']).slides[0].shapes.title.text=='New slide';results['pptx_edit']=r['verification']['status']
notebook=nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell('print(2+3)')]);p=source/'example.ipynb';nbformat.write(notebook,p)
r=process_edit(req(p,'notebook.execute',{'execute_authorized':True}));assert r['status']=='ready',r;results['notebook_execute']='5' in json.dumps(nbformat.read(r['artifacts'][0]['path'],as_version=4))
print(json.dumps({'root':str(root),'checks':results},ensure_ascii=False))
