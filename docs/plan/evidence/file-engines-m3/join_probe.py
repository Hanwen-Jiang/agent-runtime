import tempfile,json
from pathlib import Path
from agentruntime_worker.files.compute import process_compute
r=Path(tempfile.mkdtemp());i=r/'in';i.mkdir();(i/'a.csv').write_text('id,price\na,2\nb,4\n');(i/'b.csv').write_text('id,qty,secret\na,3,no\nb,5,no\n')
request={'operation':'table.query','path':str(i/'a.csv'),'input_root':str(i),'output_dir':str(r/'out'),'options':{'tables':[{'path':str(i/'b.csv'),'alias':'orders','selector':{'columns':['id','qty']}}],'sql':'SELECT sum(price*qty) total FROM data JOIN orders USING(id)'}}
x=process_compute(request);assert x['rows']==[[26]],x;assert len(x['computation']['inputs'])==2;print(json.dumps({'join':x['rows'],'inputs':x['computation']['inputs']}))
