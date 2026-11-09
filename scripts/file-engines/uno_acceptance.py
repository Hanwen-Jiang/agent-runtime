#!/usr/bin/env python3
"""Actual Linux UNO acceptance; no commercial model or production service used."""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import platform
import os
import time
import subprocess
import zipfile
import xml.etree.ElementTree as ET
from openpyxl.workbook.defined_name import DefinedName
import openpyxl
from openpyxl.styles import Font, PatternFill
from openpyxl.chart import BarChart, Reference
from openpyxl.worksheet.table import Table, TableStyleInfo
from openpyxl.utils.datetime import CALENDAR_MAC_1904
from agentruntime_worker.files.recalculate import process_recalculate
from agentruntime_worker.files.models import FileEngineError

parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
root=args.output.resolve();root.mkdir(parents=True,exist_ok=True);inputs=root/'inputs';inputs.mkdir(exist_ok=True)
checks={};evidence={}
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def execute(path,label,expected=None):
 return process_recalculate({'operation':'table.recalculate','path':str(path),'input_root':str(inputs),'output_dir':str(root/label),'options':{'base_sha256':sha(path),'timeout_seconds':90,'expected_values':expected or []}})
def expected(cell,value,sheet='Formulas'):return {'sheet':sheet,'cell':cell,'value':value,'tolerance':1e-9}

def fixture(path,epoch1904=False):
 w=openpyxl.Workbook();s=w.active;s.title='Formulas';s.append(['Category','Value']);s.append(['a',2]);s.append(['b',3]);s.append(['a',5]);s.append(['b',0])
 if epoch1904:w.epoch=CALENDAR_MAC_1904
 functions={'D1':('=SUM(B2:B5)',10),'D2':('=AVERAGE(B2:B5)',2.5),'D3':('=COUNTIF(A2:A5,"a")',2),
 'D4':('=SUMIFS(B2:B5,A2:A5,"a")',7),'D5':('=IFERROR(1/0,99)',99),'D6':('=ROUND(10/3,2)',3.33),
 'D7':('=INDEX(B2:B5,MATCH("b",A2:A5,0))',3),'D8':('=VLOOKUP("a",A2:B5,2,FALSE)',2),
 'D9':('=COUNTIFS(A2:A5,"b",B2:B5,">0")',1),'D10':('=IF(SUM(B2:B5)=10,TRUE,FALSE)',True),
 'D11':('=LEN("数据")',2),'D12':('=SUM(B2:B5)*25%',2.5),'D13':('=YEAR(DATE(2024,2,29))',2024),
 'D14':('=DAY(DATE(2024,2,29)+1)',1),'D15':('=IF(TRUE,"","x")',''),
 'D16':('=SUMPRODUCT(B2:B5,B2:B5)',38), 'D17':('=_xlfn.TEXTJOIN("-",TRUE,A2:A4)','a-b-a'),
 'D18':('=SUM(B2,B3)+SUM(B4:B5)',10),'D19':('=SUM(Values)',10),'D20':('=Untouched!B1+SUM(B2:B5)',133),'D21':('=VALUE("12.5")+1',13.5)}
 for cell,(formula,value) in functions.items():s[cell]=formula
 s['F1']=dt.datetime(2024,2,29);s['F1'].number_format='yyyy-mm-dd';s['F2']='=F1+1';s['F2'].number_format='yyyy-mm-dd'
 s['B2'].number_format='0.00';s['B2'].font=Font(bold=True,color='FF0000');s['B2'].fill=PatternFill('solid',fgColor='FFFF00')
 w.create_sheet('Untouched')['A1']='保持不变';w['Untouched']['B1']=123;w['Untouched'].sheet_state='hidden'
 s.merge_cells('A25:B25');s['A25']='Merged label'
 chart=BarChart();chart.add_data(Reference(s,min_col=2,min_row=1,max_row=5),titles_from_data=True);s.add_chart(chart,'G3')
 w.defined_names.add(DefinedName('Values',attr_text="'Formulas'!$B$2:$B$5"))
 w.save(path)
 # Poison every saved formula cache, proving calculateAll changes persisted
 # values instead of merely converting or copying a workbook.
 with zipfile.ZipFile(path) as old:members=[(info,old.read(info.filename)) for info in old.infolist()]
 ns={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
 with zipfile.ZipFile(path,'w') as new:
  for info,data in members:
   if info.filename.startswith('xl/worksheets/') and info.filename.endswith('.xml'):
    tree=ET.fromstring(data)
    for cell in tree.findall('.//s:c',ns):
     if cell.find('s:f',ns) is not None:
      value=cell.find('s:v',ns)
      if value is None:value=ET.SubElement(cell,'{'+ns['s']+'}v')
      value.text='999'
    data=ET.tostring(tree,encoding='utf-8')
   new.writestr(info,data)
 return [expected(cell,value) for cell,(_,value) in functions.items()]

for epoch in (False,True):
 source=inputs/('functions-1904.xlsx' if epoch else 'functions-1900.xlsx');wanted=fixture(source,epoch);before=sha(source)
 result=execute(source,'calc-1904' if epoch else 'calc-1900',wanted);report=result['verification'];evidence[source.stem]=report
 assert result['status']=='ready' and report['passed'] and report['calculate_all'],report
 assert sha(source)==before,'original changed'
 output=Path(result['candidate']['path']);values=openpyxl.load_workbook(output,data_only=True);formulas=openpyxl.load_workbook(output,data_only=False)
 assert values['Formulas']['F2'].value==dt.datetime(2024,3,1),values['Formulas']['F2'].value
 assert values['Untouched']['A1'].value=='保持不变' and values['Untouched']['B1'].value==123
 assert formulas['Untouched'].sheet_state=='hidden' and formulas['Formulas']['B2'].font.bold
 assert formulas['Formulas']['B2'].number_format=='0.00'
 assert 'A25:B25' in formulas['Formulas'].merged_cells and formulas['Formulas']['A25'].value=='Merged label'
 assert len(formulas['Formulas']._charts)==1, 'chart was lost'
 assert len(formulas['Formulas']._charts[0].series)==1
 assert len(report['cells'])==22 and report['formula_count']==22
 values.close();formulas.close();checks[source.stem]='22 formulas evaluated from incorrect caches, dates/names/chain verified, source/hidden-sheet/style preserved'

structured=inputs/'structured-table.xlsx';w=openpyxl.Workbook();s=w.active;s.title='Formulas';s.append(['Item','Amount']);s.append(['first',5]);s.append(['second',7])
table=Table(displayName='Table1',ref='A1:B3');table.tableStyleInfo=TableStyleInfo(name='TableStyleMedium9',showRowStripes=True);s.add_table(table);s['D1']='=SUM(Table1[Amount])';w.save(structured)
original=sha(structured);result=execute(structured,'structured-table',[expected('D1',12)])
assert result['verification']['passed'] and result['verification']['cells'][0]['evaluated_value']==12,result
assert sha(structured)==original
checks['structured_table']='local Table1[Amount] recalculated to 12 without external-link classification';evidence['structured_table']=result['verification']

error_file=inputs/'formula-error.xlsx';w=openpyxl.Workbook();w.active['A1']='=1/0';w.save(error_file);result=execute(error_file,'error')
assert result['status']=='partial' and not result['verification']['passed'] and result['verification']['formula_errors'];checks['formula_error']='failed verification, not publishable';evidence['formula_error']=result['verification']
source=inputs/'functions-1900.xlsx';wrong=execute(source,'wrong-expected',[expected('D1',999)]);assert not wrong['verification']['passed'] and wrong['verification']['value_mismatches'];checks['wrong_expected_value']='rejected'
modern=inputs/'xlookup.xlsx';w=openpyxl.Workbook();s=w.active;s.title='Formulas';s.append(['a',2]);s.append(['b',3]);s['D1']='=_xlfn.XLOOKUP("b",A1:A2,B1:B2)';w.save(modern)
result=execute(modern,'xlookup',[expected('D1',3)])
assert result['verification']['passed'] or result['verification']['formula_errors'],result
checks['xlookup']='supported, result=3' if result['verification']['passed'] else 'unsupported by installed Calc, explicit formula error'
evidence['xlookup']=result['verification']

for label,formula in [('circular','=A1+1'),('unknown-function','=CUSTOM_UNAVAILABLE(1)')]:
 path=inputs/(label+'.xlsx');w=openpyxl.Workbook();w.active['A1']=formula;w.save(path);result=execute(path,label)
 assert not result['verification']['passed'] and result['verification']['formula_errors'],result
 checks[label]='explicit formula error, no verified result'
for label,member in [('external-links','xl/externalLinks/externalLink1.xml'),('macro','xl/vbaProject.bin')]:
 path=inputs/(label+'.xlsx')
 with zipfile.ZipFile(source) as old,zipfile.ZipFile(path,'w') as new:
  for item in old.infolist():new.writestr(item,old.read(item.filename))
  new.writestr(member,b'<externalLink/>' if label=='external-links' else b'inert macro fixture - never executed')
 try:execute(path,label);raise AssertionError(label+' accepted')
 except FileEngineError as error:
  wanted='external_formula_dependency' if label=='external-links' else 'macro_workbook_unsupported'
  assert error.code==wanted,error.code;checks[label]=error.code
# A deliberately nonresponsive UNO Python process exercises the deadline and
# private soffice process-group cleanup. It cannot touch the production service.
helper=root/'slow-uno-python';helper.write_text('#!/bin/sh\nexec /usr/bin/python3 -c "import time; time.sleep(60)"\n');helper.chmod(0o755)
previous=os.environ.get('AR_UNO_PYTHON');os.environ['AR_UNO_PYTHON']=str(helper)
try:
 start=time.monotonic()
 try:
  process_recalculate({'operation':'table.recalculate','path':str(source),'input_root':str(inputs),'output_dir':str(root/'timeout'),'options':{'base_sha256':sha(source),'timeout_seconds':5}})
  raise AssertionError('timeout was accepted')
 except FileEngineError as error:
  assert error.code=='recalculation_timeout',error.code
  assert time.monotonic()-start<15
finally:
 if previous is None:os.environ.pop('AR_UNO_PYTHON',None)
 else:os.environ['AR_UNO_PYTHON']=previous
time.sleep(.5)
leftovers=[]
for path in Path('/proc').glob('[0-9]*/cmdline'):
 try:
  command=path.read_bytes()
  if b'soffice' in command and b'file-uno-' in command:leftovers.append(str(path))
 except (FileNotFoundError,PermissionError):pass
assert not leftovers,leftovers
checks['timeout_cleanup']='deadline rejected, no residual private soffice processes'
for label,formula in [('external-formula',"='[private.xlsx]Sheet1'!A1"),('data-service','=WEBSERVICE("https://example.invalid/never-fetch")')]:
 path=inputs/(label+'.xlsx');w=openpyxl.Workbook();w.active['A1']=formula;w.save(path)
 try:execute(path,label);raise AssertionError(label+' accepted')
 except FileEngineError as error:assert error.code=='external_formula_dependency',error.code
 checks[label]='rejected before opening in UNO'
assert not list(root.glob('**/macro-marker'))
report={'platform':platform.platform(),'python':platform.python_version(),'libreoffice':subprocess.check_output(['/usr/bin/soffice','--version'],text=True).strip(),'checks':checks,'reports':evidence,'processor_sha256':sha(Path(__import__('agentruntime_worker.files.recalculate',fromlist=['__file__']).__file__))}
(root/'uno-results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str))
print(json.dumps({'checks':checks,'libreoffice':report['libreoffice'],'evidence':str(root/'uno-results.json')},ensure_ascii=False))
