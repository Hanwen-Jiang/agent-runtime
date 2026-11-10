"""Run generated UI-contract fixtures against the installed immutable image.

Mount this directory and its parent as /probes, writable evidence as /evidence.
No checkout is imported for parsers; query adapter validation runs separately.
"""
import hashlib
import json
import os
from pathlib import Path
import runpy

import agentruntime_worker.files.cli as engine

assert Path(engine.__file__).resolve().is_relative_to('/opt/engine'), engine.__file__
root = Path('/evidence')
root.mkdir(exist_ok=True)
os.environ['FILE_UI_OFFICE_RESULT'] = str(root/'office-preview-results.json')
os.environ['FILE_UI_SELECTOR_ROOT'] = str(root/'selector-work')
runpy.run_path('/probes/office_preview_probe.py', run_name='__main__')
runpy.run_path('/probes/selector_probe.py', run_name='__main__')

source = root/'selector-work/input'
original_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir() if p.is_file()}
(root/'source-hashes.json').write_text(json.dumps(original_hashes,indent=2)+'\n')
module_dir = Path(engine.__file__).parent
modules = {name: hashlib.sha256((module_dir/name).read_bytes()).hexdigest() for name in ('models.py','parse.py','render.py','compute.py','edit.py','recalculate.py','inspect.py','cli.py')}
(root/'engine-module-sha256.json').write_text(json.dumps(modules,indent=2)+'\n')
# Anchor identity must be computed from the final original-source coordinates.
from agentruntime_worker.files.models import Context
from agentruntime_worker.files.cli import process
checks=[]
for name in ('region.png','region.pdf'):
    path=source/name
    selector={'bbox':[.2,.25,.8,.75]}
    if name.endswith('.pdf'):selector['page_index']=0
    request={'operation':'read','path':str(path),'input_root':str(source),'output_dir':str(root/('identity-'+name)),'selector':selector,'options':{'ocr':True}}
    response=process(request)
    assert response['blocks'] and 'REGION 42' in ' '.join(b['text'] for b in response['blocks']), response
    context=Context.from_request({**request,'output_dir':str(root/('identity-check-'+name))})
    for block in response['blocks']:
        assert block['id']==context.block(block['kind'],block['text'],block['anchor'])['id']
    assert original_hashes[name]==hashlib.sha256(path.read_bytes()).hexdigest()
    checks.append({'source':name,'recognized_text':[b['text'] for b in response['blocks']],'anchor_identity_matches':True,'source_unchanged':True})
(root/'ocr-identity-results.json').write_text(json.dumps(checks,indent=2)+'\n')
print('PASS: image-owned Office/Poppler, mixed DOCX selectors and real OCR original-coordinate identity')
