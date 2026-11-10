from pathlib import Path
import json,tempfile,sys,os
sys.path.insert(0,str(Path.cwd()/'services/py-worker/src'))
from docx import Document
from openpyxl import Workbook
from PIL import Image,ImageDraw,ImageFont
from agentruntime_worker.files.cli import process
root=Path(os.environ.get('FILE_UI_SELECTOR_ROOT','/tmp/file-ui-selector-data'));root.mkdir(exist_ok=True);inp=root/'input';inp.mkdir(exist_ok=True)
a=Workbook();a.active.title='Hidden';a.active.append(['id','price']);a.active.append([1,999]);p=a.create_sheet('Public');p.append(['id','price']);p.append([1,2]);p.append([2,4]);a.save(inp/'a.xlsx')
b=Workbook();b.active.title='Wrong';b.active.append(['id','qty']);b.active.append([1,999]);p=b.create_sheet('Lookup');p.append(['id','qty']);p.append([1,3]);p.append([2,5]);b.save(inp/'b.xlsx')
d=Document();d.add_paragraph('First');d.add_table(rows=1,cols=1).cell(0,0).text='Table between';d.add_paragraph('Last');d.save(inp/'text.docx')
raw=process({'operation':'read','path':str(inp/'text.docx'),'input_root':str(inp),'output_dir':str(root/'docx-read')})
assert raw['blocks'][2]['anchor']['paragraph_index']==1 and raw['blocks'][1]['anchor']['table_index']==0
selected=process({'operation':'read','path':str(inp/'text.docx'),'input_root':str(inp),'output_dir':str(root/'docx-selected'),'selector':{'paragraph_index':1}})
assert len(selected['blocks'])==1 and selected['blocks'][0]['text']=='Last'
image=Image.new('RGB',(1000,700),'white');draw=ImageDraw.Draw(image);draw.text((300,300),'REGION 42',fill='black',font=ImageFont.load_default(size=54));image.save(inp/'region.png');image.save(inp/'region.pdf','PDF')
checks=[{'name':'docx_body_block_paragraph_and_table_indices','passed':True}]
for name in ['region.png','region.pdf']:
 selector={'bbox':[.2,.25,.8,.75]}
 if name.endswith('.pdf'):selector['page_index']=0
 result=process({'operation':'read','path':str(inp/name),'input_root':str(inp),'output_dir':str(root/('ocr-'+name)),'selector':selector,'options':{'ocr':True}})
 assert result['blocks'],result
 for block in result['blocks']:
  box=block['anchor']['bbox'];assert .2<=box[0]<box[2]<=.8 and .25<=box[1]<box[3]<=.75
 checks.append({'name':name+'_crop_ocr_original_coordinates','passed':True,'text':[block['text'] for block in result['blocks']],'bbox':[block['anchor']['bbox'] for block in result['blocks']]})
(root/'checks.json').write_text(json.dumps(checks,indent=2)+'\n');print(json.dumps(checks))
