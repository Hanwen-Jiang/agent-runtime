#!/usr/bin/env python3
"""Reproducible M1 manual acceptance. Creates its own non-private fixtures."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import zipfile

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'services/py-worker/src'))
from agentruntime_worker.files import process
from agentruntime_worker.files.models import engine_manifest

parser=argparse.ArgumentParser();parser.add_argument('--evidence',type=Path,required=True);args=parser.parse_args()
report={'engine':engine_manifest(),'checks':[]}
with tempfile.TemporaryDirectory(prefix='file-engines-acceptance-') as directory:
    root=Path(directory);inputs=root/'inputs';inputs.mkdir();outputs=root/'outputs';outputs.mkdir()
    from PIL import Image,ImageDraw,ImageFont
    from docx import Document
    from openpyxl import Workbook
    from pptx import Presentation
    from pptx.util import Inches
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject,NameObject,DecodedStreamObject
    (inputs/'notes.txt').write_text('Header\nA real document\nTotal 42\n')
    (inputs/'data.csv').write_text('name,value\nalpha,10\nbeta,32\n')
    (inputs/'data.json').write_text(json.dumps({'total':42}))
    doc=Document();doc.add_heading('Verification report',0);doc.add_paragraph('The total is 42.');table=doc.add_table(rows=2,cols=2)
    for cell,text in zip([c for row in table.rows for c in row.cells],['Name','Value','Total','42']):cell.text=text
    doc.save(inputs/'report.docx')
    wb=Workbook();ws=wb.active;ws.title='Data';ws.append(['Item','Value']);ws.append(['A',10]);ws.append(['B',32]);ws['B4']='=SUM(B2:B3)';wb.save(inputs/'data.xlsx')
    prs=Presentation();slide=prs.slides.add_slide(prs.slide_layouts[5]);slide.shapes.title.text='Actual slide title';slide.shapes.add_textbox(Inches(1),Inches(2),Inches(5),Inches(1)).text='The answer is 42';prs.save(inputs/'slides.pptx')
    writer=PdfWriter();page=writer.add_blank_page(width=612,height=792)
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
    page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
    stream=DecodedStreamObject();stream.set_data(b'BT /F1 24 Tf 50 700 Td (Verification report) Tj 0 -50 Td (The total is 42.) Tj ET');page[NameObject('/Contents')]=writer._add_object(stream)
    with (inputs/'native.pdf').open('wb') as handle:writer.write(handle)
    fonts=[Path(os.environ.get('FILE_ENGINES_TEST_FONT','/nonexistent')),Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'),Path('/System/Library/Fonts/Supplemental/Arial.ttf')]
    font=ImageFont.truetype(str(next(path for path in fonts if path.is_file())),70)
    image=Image.new('RGB',(1400,500),'white');ImageDraw.Draw(image).text((40,100),'FILE ENGINE TEST 42',font=font,fill='black');image.save(inputs/'scan.png');image.save(inputs/'scan.pdf','PDF',resolution=144)
    (inputs/'safe.ipynb').write_text(json.dumps({'nbformat':4,'nbformat_minor':5,'metadata':{},'cells':[{'id':'a','cell_type':'code','metadata':{},'execution_count':None,'source':['open("EXECUTION_BUG", "w").write("bad")'],'outputs':[]}]}))
    with zipfile.ZipFile(inputs/'safe.zip','w') as archive:archive.writestr('docs/readme.txt','archived answer 42')
    with zipfile.ZipFile(inputs/'unsafe.zip','w') as archive:archive.writestr('../escape.txt','rejected')
    with tarfile.open(inputs/'unsafe.tar','w') as archive:
        member=tarfile.TarInfo('evil');member.type=tarfile.SYMTYPE;member.linkname='/etc/passwd';archive.addfile(member)
    source_hashes={path.name:hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs.iterdir()}
    def run(label,name,operation='read',selector=None,options=None,expected=None,error=None):
        result=process({'operation':operation,'path':str(inputs/name),'input_root':str(inputs),'output_dir':str(outputs/label),'selector':selector or {},'options':options or {}})
        if error:
            assert result.get('error',{}).get('code')==error,(label,result)
        else:
            assert result['status'] in ('ready','partial'),(label,result)
            if expected:assert expected in '\n'.join(block['text'] for block in result['blocks']),(label,result)
            for artifact in result['artifacts']:
                data=Path(artifact['path']).read_bytes();assert artifact['sha256']==hashlib.sha256(data).hexdigest()
        report['checks'].append({'name':label,'status':'passed','operation_status':result['status'],'blocks':len(result['blocks']),'artifacts':len(result['artifacts']),'coverage':result.get('coverage',{})})
        return result
    run('text','notes.txt',expected='Total 42')
    selected=run('line-selection','notes.txt',selector={'start_line':2,'max_lines':1},expected='A real document');assert selected['status']=='partial'
    run('json','data.json',expected='42');run('csv','data.csv',expected='beta\t32')
    run('docx','report.docx',expected='Name\tValue\nTotal\t42')
    sheet=run('xlsx','data.xlsx',selector={'sheet':'Data','range':'A1:B4'},expected='=SUM(B2:B3)');assert any(b.get('calculation_status')=='not_recalculated' for b in sheet['blocks'])
    run('pptx','slides.pptx',expected='Actual slide title')
    run('notebook-safe-read','safe.ipynb',expected='EXECUTION_BUG');assert not (root/'EXECUTION_BUG').exists()
    run('archive-extract','safe.zip','extract',{'members':['docs/readme.txt']})
    run('archive-traversal','unsafe.zip','archive_list',error='unsafe_archive_member')
    run('archive-link','unsafe.tar','archive_list',error='unsafe_archive_member')
    run('pdf-native','native.pdf',expected='The total is 42.')
    run('pdf-page-render','native.pdf','render',{'page_index':0})
    run('image-crop','scan.png','render',{'bbox':[0,.1,1,.7]})
    run('image-real-ocr','scan.png',options={'ocr':True},expected='FILE ENGINE TEST 42')
    run('pdf-real-ocr','scan.pdf',expected='FILE ENGINE TEST 42')
    run('docling-native-layout','native.pdf',options={'layout':'complex','ocr':False},expected='The total is 42.')
    run('docling-scan-layout-ocr','scan.pdf',options={'layout':'complex'},expected='FILE ENGINE TEST 42')
    run('office-pdf-render','report.docx','render')
    req={'operation':'read','path':str(inputs/'notes.txt'),'input_root':str(inputs),'output_dir':str(outputs/'cli')}
    env={**os.environ,'PYTHONPATH':str(ROOT/'services/py-worker/src')}
    cli=subprocess.run([sys.executable,'-m','agentruntime_worker.files.cli'],input=json.dumps(req),text=True,capture_output=True,env=env,check=True)
    assert json.loads(cli.stdout)['status']=='ready';report['checks'].append({'name':'cli-one-json-stdout','status':'passed'})
    for path in inputs.iterdir():assert hashlib.sha256(path.read_bytes()).hexdigest()==source_hashes[path.name]
    report['checks'].append({'name':'all-original-bytes-unchanged','status':'passed'})
report['status']='passed';args.evidence.parent.mkdir(parents=True,exist_ok=True);args.evidence.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'status':report['status'],'checks':len(report['checks']),'evidence':str(args.evidence)}))
