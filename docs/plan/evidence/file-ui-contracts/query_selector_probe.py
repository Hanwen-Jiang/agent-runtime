import sys,json,os
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'services/py-worker/src'))
from agentruntime_worker.files.activity import query_input_options
from agentruntime_worker.files.scopes import restrict
from agentruntime_core.errors import ProviderError
root=Path(os.environ.get('FILE_UI_SELECTOR_ROOT','/tmp/file-ui-selector-data'));inp=root/'input';selectors=[{'sheet':'Public','range':'A1:B3'},{'sheet':'Lookup','range':'A1:B3'}]
blobs=[(str(inp/'a.xlsx'),b''),(str(inp/'b.xlsx'),b'')]
opts=query_input_options({'sql':'SELECT SUM(data.price * data_2.qty) AS total FROM data JOIN data_2 USING(id)','input_selectors':selectors,'input_headers':[True,True]},selectors[0],blobs)
assert opts['tables'][0]['selector']==selectors[1] and opts['tables'][0]['path']==blobs[1][0] and opts['header'] is True
for bad in [{'input_selectors':[{}]},{'input_headers':[True,'false']},{'input_selectors':[selectors[0],{'path':'/private'}]},{'input_selectors':[{},selectors[1]]}]:
 try:query_input_options(bad,selectors[0],blobs);raise AssertionError('invalid mapping accepted')
 except ProviderError:pass
for key in ['input_selectors','input_headers']:
 try:restrict('table_query',{'path':'obj://t1/uploads/source','selector':{'sheet':'Public'},'options':{key:[]}},[{'path':'obj://t1/uploads/source','selector':{'sheet':'Public'}}]);raise AssertionError('child widened scope')
 except ProviderError:pass
(root/'query.json').write_text(json.dumps({'operation':'table.query','path':str(inp/'a.xlsx'),'input_root':str(inp),'output_dir':str(root/'query-output'),'selector':selectors[0],'options':opts}))
print('PASS: trusted input range/header mapping; invalid lists/paths and delegated-child options denied')
