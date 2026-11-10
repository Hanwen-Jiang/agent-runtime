"""Execute the trusted bridge-produced request with image-owned DuckDB."""
import hashlib,json
from pathlib import Path
import agentruntime_worker.files.cli as engine
assert Path(engine.__file__).resolve().is_relative_to('/opt/engine'), engine.__file__
root=Path('/evidence');work=root/'selector-work';request=json.loads((work/'query.json').read_text())
result=engine.process(request)
assert result['status'] in ('ready','completed'),result
assert result['columns']==['total'],result
assert result['rows']==[[26]],result
before=json.loads((root/'source-hashes.json').read_text())
for name in ('a.xlsx','b.xlsx'):
 assert before[name]==hashlib.sha256((work/'input'/name).read_bytes()).hexdigest()
(root/'query-selector-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print('PASS: trusted selected Public/Lookup ranges join to 26; both original workbooks unchanged')
