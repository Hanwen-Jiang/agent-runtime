import json,tempfile,copy
from pathlib import Path
import nbformat
from agentruntime_worker.files.edit import process_edit
from agentruntime_worker.files.models import FileEngineError
root=Path(tempfile.mkdtemp(prefix='file-notebook-selection-'));inputs=root/'inputs';inputs.mkdir();source=inputs/'three.ipynb'
first=nbformat.v4.new_code_cell('raise RuntimeError("must not execute first")',metadata={'private':'first'},execution_count=10,outputs=[nbformat.v4.new_output('stream',name='stdout',text='saved first\n')])
middle=nbformat.v4.new_code_cell('print(2+3)',metadata={'tag':'selected'})
last=nbformat.v4.new_code_cell('raise RuntimeError("must not execute last")',metadata={'private':'last'},execution_count=11,outputs=[nbformat.v4.new_output('stream',name='stdout',text='saved last\n')])
original=nbformat.v4.new_notebook(cells=[first,middle,last]);nbformat.write(original,source)
request={'operation':'notebook.execute','path':str(source),'input_root':str(inputs),'output_dir':str(root/'out'),'options':{'execute_authorized':True},'selector':{'cell_indices':[1]}}
result=process_edit(request);candidate=nbformat.read(result['candidate']['path'],as_version=4)
assert len(candidate.cells)==3
assert candidate.cells[0]==original.cells[0] and candidate.cells[2]==original.cells[2]
assert '5' in candidate.cells[1].outputs[0].text
assert nbformat.read(source,as_version=4)==original
for bad in [[-1],[3],[True],[1,1],[], '1']:
 altered=copy.deepcopy(request);altered['selector']['cell_indices']=bad;altered['output_dir']=str(root/'bad')
 try:process_edit(altered);raise AssertionError('invalid selector accepted')
 except FileEngineError as error:assert error.code=='invalid_selector',error.code
print(json.dumps({'preserved_unselected_cells':True,'selected_output':'5','source_unchanged':True,'invalid_selectors_rejected':6}))
