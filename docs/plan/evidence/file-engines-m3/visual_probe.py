import json,hashlib,tempfile,shutil,os
from pathlib import Path
from PIL import Image
from pptx import Presentation
from pptx.util import Inches
from openpyxl import Workbook,load_workbook
from agentruntime_worker.files.edit import process_edit
from agentruntime_worker.files.compute import process_compute
root=Path(tempfile.mkdtemp(prefix='file-m3-extra-'));inp=root/'inputs';inp.mkdir()
def req(p,operation,options,selector=None):return {'operation':operation,'path':str(p),'input_root':str(inp),'output_dir':str(root/(str(len(list(root.iterdir())))+'-out')),'options':options,'selector':selector or {}}
for name,color in [('before.png','blue'),('after.png','red')]:Image.new('RGB',(80,80),color).save(inp/name)
prs=Presentation();slide=prs.slides.add_slide(prs.slide_layouts[6]);pic=slide.shapes.add_picture(str(inp/'before.png'),Inches(1),Inches(1));prs.slides.add_slide(prs.slide_layouts[6]);p=inp/'deck.pptx';prs.save(p)
ops=[{'type':'replace_image','slide_index':0,'shape_id':pic.shape_id,'image_path':str(inp/'after.png')}];base=hashlib.sha256(p.read_bytes()).hexdigest()
r=process_edit(req(p,'document.edit_apply',{'base_sha256':base,'operations':ops,'fidelity':'L2'}));assert not r['verification']['passed'];candidate=inp/'candidate.pptx';shutil.copy(r['candidate']['path'],candidate)
verify=process_edit(req(p,'document.edit_verify',{'base_sha256':base,'candidate_sha256':r['candidate']['sha256'],'candidate_path':str(candidate),'operations':ops,'fidelity':'L2','visual_regions':[{'page_index':0,'bbox':[0,0,1,1]}]}));assert verify['verification']['passed'],verify
book=Workbook();book.active['A1']='before';p=inp/'dimension.xlsx';book.save(p)
r=process_edit(req(p,'document.edit_apply',{'base_sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'operations':[{'type':'set_cell','sheet':'Sheet','cell':'C5','value':'new'}]}));got=load_workbook(r['candidate']['path'],read_only=True);assert got.active['C5'].value=='new';got.close()
p=inp/'data.tsv';p.write_text('name\tvalue\na\t2\nb\t3\n');q=process_compute(req(p,'table.query',{'sql':'SELECT sum(value) FROM data'}));assert q['rows']==[[5]],q
print(json.dumps({'root':str(root),'checks':{'pptx_replace_image':True,'L2_render_verify':verify['verification']['visual'],'xlsx_new_dimension':True,'tsv_sum':True}},ensure_ascii=False))
