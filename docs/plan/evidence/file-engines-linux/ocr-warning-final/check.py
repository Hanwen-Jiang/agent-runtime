import hashlib, json, platform, sys
from pathlib import Path
from agentruntime_worker.files import process

root=Path('/workspace/run')
output=Path('/evidence')
if '--prepare-control' in sys.argv:
    import openpyxl
    inputs=output/'control/inputs';inputs.mkdir(parents=True,exist_ok=True)
    book=openpyxl.Workbook();book.active.title='Public';book.active['A1']='Amount';book.active['A2']=42;book.save(inputs/'normal.xlsx')
    source=inputs/'normal.xlsx'
    baseline=process({'operation':'read','path':str(source),'input_root':str(inputs),'output_dir':str(output/'control/baseline'),'selector':{'sheet':'Public'},'options':{'_restrict_sheet':True}})
    assert baseline['status']=='ready' and not baseline['warnings']
    (output/'control/baseline.json').write_text(json.dumps(baseline,ensure_ascii=False,indent=2)+'\n')
    print('baseline single-sheet control ready')
    raise SystemExit(0)
manifest=json.loads((root/'manifest.json').read_text())
freeze=json.loads((root/'freeze.json').read_text())
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
assert sha(root/'manifest.json')==freeze['manifest_sha256']
rows=json.loads((root/'parse-summary.json').read_text())['files']
misses=[row['id'] for row in rows if any(not c['text_found'] or not c['anchor_found'] for c in row['checks'])]
assert len(misses)==2,misses
byid={item['id']:item for item in manifest['files']}
checks=[]
for name in [*misses,'xlsx_00']:
    item=byid[name];source=root/'inputs'/item['path'];before=sha(source)
    old=json.loads((root/'parse'/f'{name}.json').read_text())['result']
    result=process({'operation':item['operation'],'path':str(source),'input_root':str(root/'inputs'),'output_dir':str(output/name),'selector':item['selector'],'options':item['options']})
    assert before==item['sha256']==sha(source),(name,'source changed')
    assert old['blocks']==result['blocks'],(name,'block content, IDs, confidence or anchors changed')
    assert old.get('source')==result.get('source'),(name,'source metadata changed')
    if name in misses:
        count=sum(bool(block.get('low_confidence')) for block in result['blocks'])
        assert count>0
        assert old['status']=='ready' and result['status']=='partial'
        warning=[v for v in result['warnings'] if v.get('code')=='ocr_low_confidence']
        assert len(warning)==1 and warning[0]['blocks']==count
        assert result['coverage']['ocr_low_confidence_blocks']==count
        assert {k:v for k,v in result['coverage'].items() if k!='ocr_low_confidence_blocks'}==old['coverage']
    else:
        assert old['status']==result['status']=='partial'
        assert not result['warnings'] and not old['warnings']
        count=0
    (output/f'{name}-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    checks.append({'id':name,'status_before':old['status'],'status_after':result['status'],'low_confidence_blocks':count,'blocks_identical':True,'source_unchanged':True,'sha256':before,'warning_codes':[w['code'] for w in result['warnings']]})
control=output/'control/inputs/normal.xlsx';before=sha(control)
old=json.loads((output/'control/baseline.json').read_text())
result=process({'operation':'read','path':str(control),'input_root':str(control.parent),'output_dir':str(output/'control/final'),'selector':{'sheet':'Public'},'options':{'_restrict_sheet':True}})
assert old['status']==result['status']=='ready' and not result['warnings']
assert old['blocks']==result['blocks'] and old['source']==result['source']
assert before==sha(control)
checks.append({'id':'independent_single_sheet_control','status_before':'ready','status_after':'ready','low_confidence_blocks':0,'blocks_identical':True,'source_unchanged':True,'sha256':before,'warning_codes':[]})
files=Path('/opt/engine/agentruntime_worker/files')
report={'checks':checks,'frozen_manifest_sha256':freeze['manifest_sha256'],'gold_changed':False,'parsed_corpus_overwritten':False,'baseline_results_source':'Existing frozen benchmark run/parse/*.json; no baseline reprocessing','python':platform.python_version(),'module_hashes':{p.name:sha(p) for p in sorted(files.glob('*.py'))}}
(output/'ocr-warning-check.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'passed':len(checks),'checks':checks},ensure_ascii=False))
