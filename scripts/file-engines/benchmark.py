#!/usr/bin/env python3
"""Reproducible, non-private *synthetic* file benchmark, never a human gold set.

Generate: python benchmark.py generate --work /workspace/benchmark
Parse:    python benchmark.py parse --work /workspace/benchmark
Retrieve: python benchmark.py retrieve --work /workspace/benchmark --model-dir /models/e5
Report:   python benchmark.py report --work /workspace/benchmark

Generation alone needs reportlab (benchmark dependency, not runtime dependency).
Retrieval needs llama-index-core and transformers/torch plus Runtime Python libs.
Run in the reviewed engine image, --cpus=4 --memory=8g. Preserve all JSON outputs.
"""
from __future__ import annotations
import argparse, asyncio, collections, csv, hashlib, io, json, math, os, re, statistics, subprocess, sys, time, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for relative in (os.getenv('BENCHMARK_ENGINE_SOURCE','services/py-worker/src'), 'services/py-memory/src', 'libs/py-core/src', 'proto/gen/python'):
    sys.path.insert(0, str(ROOT / relative))

# Each topic has distinct vocabulary and a paired Chinese description. Six facets
# vary across document families so disagreement/version evidence is observable.
TOPICS = [
 ('travel reimbursement','差旅报销','Finance','财务','itemized receipts','明细发票','expense portal','报销系统'),
 ('employee onboarding','员工入职','People Operations','人事','signed employment agreement','劳动合同','staff portal','员工平台'),
 ('customer refunds','客户退款','Customer Care','客服','original order confirmation','原始订单','support ticket','客服工单'),
 ('warehouse returns','仓库退货','Logistics','物流','parcel tracking record','物流单号','returns desk','退货登记台'),
 ('password recovery','密码找回','Identity Team','身份管理组','verified recovery address','已验证恢复邮箱','identity portal','身份平台'),
 ('production incident response','生产故障响应','Site Reliability','可靠性团队','incident timeline','故障时间线','incident channel','故障频道'),
 ('research data retention','科研数据保留','Research Office','科研办公室','approved study protocol','研究方案','research registry','科研登记系统'),
 ('supplier qualification','供应商准入','Procurement','采购','security questionnaire','安全问卷','supplier portal','供应商平台'),
 ('software release approval','软件发布审批','Release Board','发布委员会','passing regression report','回归测试报告','release tracker','发布登记系统'),
 ('equipment borrowing','设备借用','Facilities','设施组','asset checkout record','资产借用记录','asset desk','资产服务台'),
 ('remote work requests','远程办公申请','Team Lead','团队主管','coverage schedule','值班安排','workforce portal','考勤系统'),
 ('training reimbursement','培训报销','Learning Team','培训组','completion certificate','结课证书','learning portal','培训平台'),
 ('visitor access','访客通行','Reception','前台','host confirmation','接待人确认','visitor kiosk','访客终端'),
 ('customer data deletion','客户数据删除','Privacy Office','隐私办公室','verified deletion request','已验证删除请求','privacy portal','隐私平台'),
 ('contract renewal','合同续签','Legal Operations','法务','current contract copy','现有合同副本','contract registry','合同系统'),
 ('security vulnerability reporting','安全漏洞报告','Security Team','安全团队','reproducible proof of concept','可复现验证材料','security inbox','安全邮箱'),
 ('inventory reconciliation','库存盘点','Inventory Control','库存组','signed stock count','已签名盘点表','inventory ledger','库存台账'),
 ('backup restoration','备份恢复','Storage Team','存储团队','approved recovery ticket','恢复审批工单','recovery console','恢复控制台'),
 ('accessibility review','无障碍审查','Design Quality','设计质量组','keyboard navigation checklist','键盘导航清单','design review queue','设计审查队列'),
 ('purchase order changes','采购单变更','Purchasing','采购执行组','supplier acknowledgement','供应商确认','purchasing system','采购系统'),
]


def dump(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def load(path): return json.loads(Path(path).read_text())
def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def norm(text): return ''.join(c.lower() for c in str(text) if c.isalnum())
def environment():
    import platform,resource
    usage=resource.getrusage(resource.RUSAGE_SELF)
    return {'python':platform.python_version(),'platform':platform.platform(),'engine_source':os.getenv('BENCHMARK_ENGINE_SOURCE','repository'),'peak_rss_bytes':usage.ru_maxrss*(1 if sys.platform=='darwin' else 1024),'user_cpu_seconds':usage.ru_utime,'system_cpu_seconds':usage.ru_stime}


def percentile(values, q):
    return sorted(values)[min(len(values)-1, math.ceil(q*len(values))-1)] if values else None


def facts(topic, variant, language):
    en, zh, owner, owner_zh, proof, proof_zh, channel, channel_zh = TOPICS[topic]
    days, retention = 3 + (topic * 7 + variant * 3) % 25, 6 + (topic + variant) % 19
    if language == 'zh':
        return [f'{zh}的提交期限为{days}个工作日。', f'{zh}的审批责任人为{owner_zh}。',
            f'{zh}必须提供{proof_zh}。', f'{zh}记录须保留{retention}个月。',
            f'{zh}的紧急例外需要部门负责人书面批准。', f'{zh}必须通过{channel_zh}提交。']
    return [f'{en.capitalize()} must be submitted within {days} business days.',
        f'{owner} is responsible for approving {en}.', f'{en.capitalize()} requires {proof}.',
        f'Records for {en} must be retained for {retention} months.',
        f'Emergency exceptions for {en} require written approval from the department head.',
        f'{en.capitalize()} must be submitted through the {channel}.']


def questions(topic):
    en, zh, *_ = TOPICS[topic]
    return [f'Compare the submission deadlines for {en} across the policies.',
        f'Who signs off on {en} according to the different documents?',
        f'{zh}需要哪些凭证？比较不同资料的要求。',
        f'{zh}的记录保存多久？请找出各文件中的规定。',
        f'Which channel should I use to file {en}? Compare the documents.']


def generate(work):
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
    from docx import Document
    from openpyxl import Workbook
    from pptx import Presentation
    from pptx.util import Inches, Pt
    from pypdf import PdfReader, PdfWriter
    from reportlab.pdfgen import canvas
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    inputs = work/'inputs'; inputs.mkdir(parents=True, exist_ok=True)
    pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
    font_paths = [os.getenv('BENCHMARK_CJK_FONT',''), '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc', '/System/Library/Fonts/PingFang.ttc']
    cjk = next((p for p in font_paths if p and Path(p).is_file()), None)
    if not cjk: raise RuntimeError('Noto CJK or BENCHMARK_CJK_FONT required')
    font = ImageFont.truetype(cjk, 31)
    manifest = {'schema_version':1,'provenance':'Generated non-private policy scenarios; not real business documents; golden anchors are automatic, NOT human annotated.',
        'human_review':{'reviewed_anchors':0,'required_anchors':500,'gate':'pending'},'seed':20261110,'files':[], 'queries':[]}
    def add(kind, n, topic, variant, language, suffix, *, expected='readable'):
        ident=f'{kind}_{n:02d}'; path=inputs/(ident+suffix)
        item={'id':ident,'family':kind,'topic':topic,'language':language,'variant':variant,'path':path.name,'expected':expected,'operation':'read','selector':{},'options':{},'gold':[]}
        manifest['files'].append(item)
        return item,path,facts(topic,variant,language)
    def gold(item, texts, anchor_fn):
        item['gold']=[{'fact':k,'text':t,'anchor':anchor_fn(k)} for k,t in enumerate(texts)]
    def native_pdf(path, texts, page_count, two_column=False):
        c=canvas.Canvas(str(path),pagesize=(612,792),invariant=1)
        positions=list(range(len(texts))) if page_count==len(texts) else [0,5,11,17,23,29]
        for page in range(page_count):
            c.setFont('STSong-Light',13); c.drawString(36,756,f'Synthetic policy edition / 合成政策版本 — page {page+1}')
            if page in positions:
                text=texts[positions.index(page)]
                for line_i,line in enumerate(wrap(text, 36 if two_column else 64)):
                    c.drawString(36,700-line_i*23,line)
                if two_column:
                    c.setFont('STSong-Light',10);c.drawString(330,700,'Supplementary audit notes / 审计备注')
                    c.rect(330,600,230,75);c.drawString(340,635,'Quarter | Owner | Status')
            else: c.drawString(36,700,'Appendix: glossary and revision history / 附录与修订记录')
            c.showPage()
        c.save();return positions
    def raster(texts, low=False, rotate=False):
        im=Image.new('RGB',(1350,430+max(0,len(texts)-2)*170),'white');d=ImageDraw.Draw(im)
        y=45
        for text in texts:
            for line in wrap(text,57):d.text((38,y),line,font=font,fill='#111111');y+=48
            y+=28
        if low:im=im.resize((675,im.height//2)).filter(ImageFilter.GaussianBlur(.45)).resize((1350,im.height))
        if rotate:im=im.rotate(.8,resample=Image.Resampling.BICUBIC,fillcolor='white')
        return im
    for n in range(20):
        item,path,texts=add('text_pdf',n,n,0,'zh' if n%2 else 'en','.pdf')
        pages=native_pdf(path,texts,30 if n==0 else 6,two_column=n%4==1)
        item['selector']={'pages':pages};gold(item,texts,lambda k:{'page_index':pages[k]})
        item['options']={'layout':'complex','ocr':False} if n%4==1 else {}
        item['structural_variant']='two_column_table' if n%4==1 else ('30_page_definition_p12' if n==0 else 'headings_footers')
    for n in range(20):
        item,path,texts=add('scan_pdf',n,n,1,'en' if n%2 else 'zh','.pdf')
        writer=PdfWriter(); pages_count=20 if n==0 else 3
        gold_pages=[]
        for page in range(pages_count):
            buf=io.BytesIO()
            pair=texts[page*2:page*2+2] if page<3 else ['Appendix scan / 扫描附录']
            if n%4==3 and page==1:
                temp=inputs/(item['id']+'-native.tmp.pdf');native_pdf(temp,pair,2)
                # One mixed document has native pages as well as scanned pages.
                for p in PdfReader(temp).pages:writer.add_page(p)
                temp.unlink();gold_pages.extend([len(writer.pages)-2,len(writer.pages)-1])
            else:
                raster(pair,low=n%5==0,rotate=n%7==0).save(buf,format='PDF',resolution=144);writer.add_page(PdfReader(buf).pages[0])
                if page<3:gold_pages.extend([len(writer.pages)-1]*len(pair))
        with path.open('wb') as f:writer.write(f)
        item['selector']={'pages':sorted(set(gold_pages))};gold(item,texts,lambda k:{'page_index':gold_pages[k]})
        item['structural_variant']='mixed_native_scan' if n%4==3 else ('low_resolution' if n%5==0 else 'scan_rotated' if n%7==0 else 'scan_clean')
    for family,offset in [('docx',0),('xlsx',5),('pptx',10)]:
        for n in range(15):
            topic=(n+offset)%20;item,path,texts=add(family,n,topic,2+offset//5,'zh' if n%2 else 'en','.'+family)
            if family=='docx':
                doc=Document();doc.add_heading(f'Synthetic policy {topic} / 合成政策',0);anchors=[]
                for k,text in enumerate(texts):
                    if n%3==0 and k==3:doc.add_table(rows=1,cols=1).cell(0,0).text=text
                    else:doc.add_paragraph(text,style='Heading 2' if k==0 else None)
                    anchors.append({'block_index':k+1})
                doc.sections[0].header.paragraphs[0].text='Synthetic benchmark — not business policy'
                doc.save(path);gold(item,texts,lambda k:anchors[k]);item['structural_variant']='paragraphs_tables_headers' if n%3==0 else 'styled_paragraphs'
            elif family=='xlsx':
                wb=Workbook();ws=wb.active;ws.title='Policy';ws.append(['Facet','Clause'])
                for k,text in enumerate(texts):ws.append([k,text]);ws.cell(k+2,2).alignment=__import__('openpyxl').styles.Alignment(wrap_text=True)
                calc=wb.create_sheet('Budget');calc.append(['Amount','Rate','Total']);calc.append([100+n,1.2,'=A2*B2']);calc['A4']='=SUM(A2:A3)'
                hidden=wb.create_sheet('Reference');hidden.sheet_state='hidden';hidden['A1']='Must be preserved';wb.save(path)
                item['selector']={'sheet':'Policy','range':'A1:B7'};gold(item,texts,lambda k:{'sheet':'Policy','range':f'B{k+2}'})
                item['structural_variant']='multi_sheet_formula_hidden_reference'
            else:
                prs=Presentation();anchors=[]
                for k,text in enumerate(texts):
                    slide=prs.slides.add_slide(prs.slide_layouts[6]);shape=slide.shapes.add_textbox(Inches(.5), Inches(.6), Inches(9), Inches(3));shape.text=text
                    for para in shape.text_frame.paragraphs:para.font.size=Pt(25)
                    slide.notes_slide.notes_text_frame.text=f'Speaker note {n}/{k}: preserve this information.'
                    if k==2:
                        table=slide.shapes.add_table(2,2,Inches(.5),Inches(4),Inches(5), Inches(1)).table;table.cell(0,0).text='Review';table.cell(1,1).text='Complete'
                    anchors.append({'slide_index':k,'shape_id':shape.shape_id})
                prs.save(path);gold(item,texts,lambda k:anchors[k]);item['structural_variant']='slides_text_table_notes'
    # 35 other files: 10 text, 6 tabular, 5 images, 4 notebooks, 5 archives, 5 bad inputs.
    suffixes=['.txt','.md','.json','.jsonl','.yaml','.xml','.py','.log','.txt','.txt']
    for n,suffix in enumerate(suffixes):
        item,path,texts=add('text',n,n,5,'zh' if n%2 else 'en',suffix)
        if suffix=='.json': body=json.dumps({'clauses':texts},ensure_ascii=False,indent=2)
        elif suffix=='.jsonl':body='\n'.join(json.dumps({'clause':t},ensure_ascii=False) for t in texts)
        elif suffix=='.yaml':body='clauses:\n'+'\n'.join('  - '+t for t in texts)
        elif suffix=='.xml':body='<policy>\n'+'\n'.join('<clause>'+t+'</clause>' for t in texts)+'\n</policy>'
        elif suffix=='.py':body='\n'.join('# '+t for t in texts)+'\nraise RuntimeError("READ_MUST_NOT_EXECUTE")'
        elif suffix=='.md':body='# Policy / 政策\n\n'+'\n\n'.join(texts)
        else:body='\n'.join(texts)
        encoding='utf-16' if n==8 else 'gb18030' if n==9 else 'utf-8';path.write_text(body,encoding=encoding)
        item['options']={'encoding':encoding};gold(item,texts,lambda k:{'line_contains':next(i+1 for i,line in enumerate(body.splitlines()) if texts[k] in line)})
        item['structural_variant']=suffix[1:]+'_'+encoding
    for n in range(6):
        suffix='.csv' if n<4 else '.tsv';item,path,texts=add('table',n,n+10,6,'en' if n%2 else 'zh',suffix)
        with path.open('w',newline='') as f:
            writer=csv.writer(f,delimiter='\t' if suffix=='.tsv' else ',');writer.writerow(['id','clause','amount','region'])
            for k in range(100000 if n==0 else 6):writer.writerow([k,texts[k] if k<6 else f'Inventory ledger row {k}',k%100,'east' if k%2==0 else 'west'])
        item['selector']={'count':6};gold(item,texts,lambda k:{'row_index':k});item['structural_variant']='100000_rows_exact_query' if n==0 else 'quoted_delimited_table'
    for n in range(5):
        suffix=['.png','.jpg','.webp','.png','.png'][n];item,path,texts=add('image',n,n+15,7,'zh' if n%2 else 'en',suffix)
        raster(texts,low=n==3,rotate=n==4).save(path);item['options']={'ocr':True};gold(item,texts,lambda k:{'kind':'image_region'});item['structural_variant']='low_resolution' if n==3 else 'rotated' if n==4 else 'raster_'+suffix[1:]
    for n in range(4):
        item,path,texts=add('notebook',n,n+3,8,'zh' if n%2 else 'en','.ipynb')
        cells=[{'id':f'clause-{k}','cell_type':'markdown','metadata':{},'source':[t]} for k,t in enumerate(texts)]
        cells.append({'id':'do-not-run','cell_type':'code','metadata':{},'source':['from pathlib import Path\nPath("NOTEBOOK_EXECUTED").write_text("unsafe")'],'execution_count':None,'outputs':[]})
        dump(path,{'nbformat':4,'nbformat_minor':5,'metadata':{},'cells':cells});gold(item,texts,lambda k:{'cell_index':k,'cell_id':f'clause-{k}'})
        item['structural_variant']='markdown_code_read_without_execution'
    for n in range(5):
        item,path,texts=add('archive',n,n+10,9,'en','.zip');members=[]
        with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as z:
            for k,text in enumerate(texts):
                member=f'policies/{TOPICS[n+10][0].replace(" ","-")}/clause-{k+1}.txt';z.writestr(member,text);members.append(member)
        gold(item,members,lambda k:{'member':members[k]});item['retrieval_excluded']='archive listing is not member content';item['structural_variant']='nested_members_explicit_extract'
    for n in range(5):
        suffix=['.zip','.zip','.pdf','.docx','.bin'][n];item,path,_=add('abnormal',n,n,10,'en',suffix,expected='rejected')
        if n<2:
            with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as z:z.writestr('../escape.txt' if n==0 else 'bomb.txt','outside' if n==0 else '0'*(2<<20))
        elif n==2:
            w=PdfWriter();w.add_blank_page(612,792);w.encrypt('benchmark-only');w.write(str(path))
        elif n==3:path.write_bytes(b'PK\x03\x04not-a-valid-office-container')
        else:path.write_bytes(bytes(range(256))*20)
        item['structural_variant']=['path_traversal','compression_ratio','encrypted_pdf','corrupt_docx','unsupported_binary'][n]
    for item in manifest['files']:item['sha256']=digest(inputs/item['path']);item['size_bytes']=(inputs/item['path']).stat().st_size
    for topic in range(20):
        expected=[f['id'] for f in manifest['files'] if f['topic']==topic and f['gold'] and not f.get('retrieval_excluded')]
        for qi,q in enumerate(questions(topic)):
            manifest['queries'].append({'id':f'q{topic:02d}_{qi}','query':q,'topic':topic,'fact':[0,1,2,3,5][qi],'expected_files':expected,'language':'zh' if qi in (2,3) else 'en'})
    assert len(manifest['files'])==120 and len(manifest['queries'])==100
    dump(work/'manifest.json',manifest)
    print(json.dumps({'files':120,'golden_anchors':sum(len(f['gold']) for f in manifest['files']),'queries':100,'human_reviewed':0,'families':dict(collections.Counter(f['family'] for f in manifest['files']))}))


def wrap(text, width):
    # Character-count wrapping supports CJK while preserving words in Latin text.
    if ' ' not in text:return [text[i:i+width] for i in range(0,len(text),width)]
    import textwrap
    return textwrap.wrap(text,width=width,break_long_words=False)


def anchor_matches(expected, actual):
    for key,value in expected.items():
        if key=='line_contains':
            if not actual.get('start_line',0)<=value<=actual.get('end_line',0):return False
        elif actual.get(key)!=value:return False
    box=actual.get('bbox')
    return box is None or (len(box)==4 and all(isinstance(v,(int,float)) and 0<=v<=1.001 for v in box) and box[0]<=box[2] and box[1]<=box[3])


def independent_check(path,item):
    """Source-ground-truth audit independent of the production parser where possible.
    Does not convert automatically reviewed truth into human labels.
    """
    import xml.etree.ElementTree as ET
    family=item['family'];texts=[]
    if family in ('docx','xlsx','pptx'):
        with zipfile.ZipFile(path) as z:
            names=[n for n in z.namelist() if n.endswith('.xml') and ((family=='docx' and n=='word/document.xml') or (family=='xlsx' and n.startswith('xl/worksheets/')) or (family=='pptx' and n.startswith('ppt/slides/slide')))]
            for name in names:
                root=ET.fromstring(z.read(name));texts.extend(''.join(node.itertext()) for node in root.iter() if node.tag.rsplit('}',1)[-1] in ('t','v'))
    elif family=='text_pdf':
        p=subprocess.run(['pdftotext',str(path),'-'],capture_output=True,text=True)
        if p.returncode:return {'method':'poppler_pdftotext','verified':False,'error':p.stderr[:300]}
        texts=[p.stdout]
    elif family=='notebook':texts=[''.join(c['source']) for c in load(path)['cells']]
    elif family=='table':
        with path.open() as f:texts=[' '.join(row) for _,row in zip(range(8),csv.reader(f,delimiter='\t' if path.suffix=='.tsv' else ','))]
    elif family=='text':texts=[path.read_text(encoding=item['options']['encoding'])]
    elif family=='archive':
        with zipfile.ZipFile(path) as z:texts=z.namelist()
    else:return {'method':'raster_requires_human_visual_review' if family in ('scan_pdf','image') else 'negative_case','verified':None}
    haystack=norm(' '.join(texts));verified=sum(norm(g['text']) in haystack for g in item['gold'])
    return {'method':'raw_ooxml' if family in ('docx','xlsx','pptx') else 'poppler_pdftotext' if family=='text_pdf' else 'source_structure','verified':verified==len(item['gold']),'found':verified,'total':len(item['gold'])}


def verify_frozen(work):
    freeze=load(work/'freeze.json') if (work/'freeze.json').exists() else None
    if freeze and digest(work/'manifest.json')!=freeze['manifest_sha256']:
        raise RuntimeError('Frozen golden manifest changed; create a separately versioned evaluation instead')
    return freeze


def parse(work, only=None):
    verify_frozen(work)
    from agentruntime_worker.files import process
    import difflib
    manifest=load(work/'manifest.json');out=work/'parse';out.mkdir(exist_ok=True)
    records=[]
    for item in manifest['files']:
        if only and item['family'] not in only.split(','):continue
        path=work/'inputs'/item['path'];started=time.perf_counter()
        try:
            result=process({'operation':item['operation'],'path':str(path),'input_root':str(work/'inputs'),'output_dir':str(work/'artifacts'/item['id']),'selector':item['selector'],'options':item['options']})
        except Exception as e:result={'status':'raised','error':{'code':type(e).__name__,'message':str(e)[:400]},'blocks':[]}
        elapsed=time.perf_counter()-started;checks=[]
        for gold in item['gold']:
            candidates=[b for b in result.get('blocks',[]) if anchor_matches(gold['anchor'],b.get('anchor',{}))]
            # OCR may split one sentence across lines: concatenate only same-page
            # candidates in parser order, never silently match another page.
            text=' '.join(b.get('text','') for b in candidates);needle=norm(gold['text']);hay=norm(text)
            exact=needle in hay
            best=max((difflib.SequenceMatcher(None,needle,norm(b.get('text',''))).ratio() for b in candidates),default=0)
            checks.append({'fact':gold['fact'],'text_found':exact,'anchor_found':bool(candidates),'best_block_similarity':round(best,4),'candidate_ids':[b['id'] for b in candidates if needle in norm(b.get('text',''))]})
        independent=independent_check(path,item)
        row={'id':item['id'],'family':item['family'],'variant':item['structural_variant'],'language':item['language'],'expected':item['expected'],'status':result.get('status'),'elapsed_seconds':elapsed,'checks':checks,'independent':independent,'original_unchanged':digest(path)==item['sha256'],'error':result.get('error'),'ocr':{'blocks':sum(b.get('derivation')=='ocr' for b in result.get('blocks',[])),'low_confidence_blocks':sum(bool(b.get('low_confidence')) for b in result.get('blocks',[]))},'coverage':result.get('coverage'),'warnings':result.get('warnings',[])}
        dump(out/(item['id']+'.json'),{'evaluation':row,'result':result});records.append(row)
        print(json.dumps({k:row[k] for k in ('id','status','elapsed_seconds','error')}),flush=True)
    # Supports separate batches without rewriting completed rows from other formats.
    allrows=[load(p)['evaluation'] for p in sorted(out.glob('*.json'))]
    dump(work/'parse-summary.json',{'files':allrows,'parser_total_seconds':sum(r['elapsed_seconds'] for r in allrows),'environment':environment(),'notebook_read_only':all(r.get('coverage',{}).get('executed') is False for r in allrows if r['family']=='notebook') and not any((p/'NOTEBOOK_EXECUTED').exists() for p in (Path.cwd(),work,work/'inputs')),'human_reviewed_anchors':0})
    if only is None or 'table' in only.split(','):
        aggregates=[]
        for attempt in range(2):
            result=process({'operation':'table.query','path':str(work/'inputs/table_00.csv'),'input_root':str(work/'inputs'),'output_dir':str(work/'artifacts'/f'csv_aggregate_{attempt}'),'options':{'sql':"SELECT COUNT(*) AS n, SUM(amount) AS amount FROM data WHERE region = 'east'"}})
            aggregates.append(result)
        dump(work/'exact-compute.json',{'expected':{'rows':[[50000,2450000]],'input_rows':100000},'passed':all(r.get('status')=='ready' and r.get('rows')==[[50000,2450000]] and r.get('verification',{}).get('input_rows')==100000 for r in aggregates),'attempts':aggregates})


async def retrieve(work,model_dir,reranker_dir=None):
    verify_frozen(work)
    import torch
    from transformers import AutoModel, AutoTokenizer, AutoModelForSequenceClassification
    from agentruntime_memory.retrieval import RetrievalEngine
    from agentruntime_memory.embedding import GatewayEmbedder,tokens
    from agentruntime_memory.wiring import GatewayReranker
    from agentruntime_memory.identity import Identity
    from agentruntime_core.dev.vector_store import InMemoryVectorStoreProvider
    from agentruntime_core.dev.relational_store import SQLiteRelationalStoreProvider
    from agentruntime_core.errors import NotFoundError,ConflictError
    from agentruntime.modelgateway.v1 import model_gateway_pb2 as mg
    from agentruntime.memory.v1 import knowledge_pb2 as kp, knowledge_pb2_grpc as kg
    from agentruntime.common.v1 import payload_pb2
    from agentruntime_memory.knowledge import KnowledgeService, register_knowledge
    from agentruntime_memory.identity import DevTokenAuthenticator, issue_dev_token
    from google.protobuf import json_format, struct_pb2
    import grpc
    torch.set_num_threads(4)
    model_dir=Path(model_dir)
    model_info=load(model_dir/'benchmark-model.json')
    for name,sha in model_info['sha256'].items():
        if digest(model_dir/name)!=sha:raise RuntimeError('Embedding model file hash mismatch: '+name)
    model=AutoModel.from_pretrained(model_dir,local_files_only=True).eval();tokenizer=AutoTokenizer.from_pretrained(model_dir,local_files_only=True)
    reranker_info=None
    if reranker_dir:
        reranker_dir=Path(reranker_dir);reranker_info=load(reranker_dir/'benchmark-model.json')
        for name,sha in reranker_info['sha256'].items():
            if digest(reranker_dir/name)!=sha:raise RuntimeError('Reranker hash mismatch: '+name)
        reranker=AutoModelForSequenceClassification.from_pretrained(reranker_dir,local_files_only=True).eval()
        reranker_tokenizer=AutoTokenizer.from_pretrained(reranker_dir,local_files_only=True)
    manifest=load(work/'manifest.json');items={f['id']:f for f in manifest['files']}
    class LocalGateway:
        phase='passage';calls=0;input_tokens=0;seconds=0;score_calls=0;score_pairs=0;score_tokens=0;score_seconds=0
        async def embed(self,request):
            assert request.tenant.tenant_id=='benchmark' and request.embedding_version==model_info['version']
            start=time.perf_counter();batch=tokenizer([self.phase+': '+text for text in request.texts],max_length=512,padding=True,truncation=True,return_tensors='pt')
            self.input_tokens+=int(batch['attention_mask'].sum())
            with torch.inference_mode():
                hidden=model(**batch).last_hidden_state;mask=batch['attention_mask'];pooled=hidden.masked_fill(~mask[...,None].bool(),0.).sum(1)/mask.sum(1)[...,None];emb=torch.nn.functional.normalize(pooled,p=2,dim=1)
            self.calls+=1;self.seconds+=time.perf_counter()-start
            return mg.EmbedResponse(embedding_version=model_info['version'],embeddings=[mg.Embedding(values=v.tolist()) for v in emb])
        async def score(self,request):
            assert reranker_info and request.tenant.tenant_id=='benchmark' and len(request.candidates)<=80
            start=time.perf_counter();scores=[]
            for offset in range(0,len(request.candidates),16):
                passages=list(request.candidates[offset:offset+16])
                batch=reranker_tokenizer([request.query]*len(passages),passages,max_length=512,padding=True,truncation=True,return_tensors='pt')
                self.score_tokens+=int(batch['attention_mask'].sum())
                with torch.inference_mode():scores.extend(reranker(**batch).logits.view(-1).tolist())
            self.score_calls+=1;self.score_pairs+=len(request.candidates);self.score_seconds+=time.perf_counter()-start
            return mg.ScoreResponse(scores=scores)
    gateway=LocalGateway();alice=Identity('benchmark','alice','workspace');bob=Identity('benchmark','bob','workspace')
    vectors=InMemoryVectorStoreProvider();db=SQLiteRelationalStoreProvider(work/('retrieval-db-'+str(time.time_ns())))
    docs=[];allblocks={}
    for item in manifest['files']:
        p=work/'parse'/(item['id']+'.json')
        if not p.exists() or item.get('retrieval_excluded') or not item['gold']:continue
        parsed=load(p)['result'];blocks=[b for b in parsed.get('blocks',[]) if b.get('text','').strip()]
        if not blocks:continue
        docs.append({'version_id':item['id'],'representation_id':'rep_'+item['id'],'blocks':blocks});allblocks[item['id']]={b['id']:b for b in blocks}
    allowed={d['version_id'] for d in docs}
    async def authorize(identity,collection,generation,versions):return set(versions)&allowed if identity.user_id=='alice' else set()
    engine=RetrievalEngine(vectors=vectors,embedder=GatewayEmbedder(gateway,version=model_info['version']),relational=db,authorize=authorize,rerank=GatewayReranker(gateway) if reranker_info else None)
    await engine.setup()
    server=grpc.aio.server(options=[('grpc.max_receive_message_length',24<<20),('grpc.max_send_message_length',24<<20)])
    key=b'non-secret-synthetic-benchmark';register_knowledge(server,KnowledgeService(engine),DevTokenAuthenticator(key),'synthetic-internal')
    port=server.add_insecure_port('127.0.0.1:0');await server.start()
    channel=grpc.aio.insecure_channel(f'127.0.0.1:{port}',options=[('grpc.max_receive_message_length',24<<20),('grpc.max_send_message_length',24<<20)])
    stub=kg.KnowledgeServiceStub(channel)
    async def rpc(operation,payload,internal=True):
        value=struct_pb2.Value();json_format.ParseDict(payload,value)
        token=issue_dev_token(key,tenant='benchmark',user='alice')
        metadata=[('authorization','Bearer '+token)]+([('x-ar-knowledge-token','synthetic-internal')] if internal else [])
        response=await stub.Execute(kp.KnowledgeServiceExecuteRequest(tenant=alice.tenant_context(),operation=operation,payload=payload_pb2.JsonPayload(value=value)),metadata=metadata)
        return json_format.MessageToDict(response.result.value)
    params={'collection_id':'benchmark','generation':'generation1'}
    try:await rpc('index',{**params,'documents':docs},internal=False)
    except grpc.aio.AioRpcError as e:assert e.code()==grpc.StatusCode.PERMISSION_DENIED
    else:raise AssertionError('missing internal token accepted')
    start=time.perf_counter();index=await rpc('index',{**params,'documents':docs});index_seconds=time.perf_counter()-start
    # A separate immutable generation has 20 files with two representations each;
    # this exercises duplicate version IDs without conflating representation IDs.
    multi=[]
    for doc in docs[:20]:
        midpoint=max(1,len(doc['blocks'])//2)
        for part,blocks in enumerate((doc['blocks'][:midpoint],doc['blocks'][midpoint:])):
            if blocks:multi.append({**doc,'representation_id':doc['representation_id']+f'_part{part}','blocks':blocks})
    multi_index=await rpc('index',{'collection_id':'benchmark','generation':'generation2','documents':multi})
    async with db.bind(alice.tenant_context()).transaction(read_only=True) as tx:nodes=[dict(n) for n in await tx.query('SELECT * FROM retrieval_nodes WHERE collection_id=$1 AND generation=$2',('benchmark','generation1'))]
    lexical=[(n,collections.Counter(tokens(n['body']))) for n in nodes]
    df=collections.Counter(t for _,c in lexical for t in c);avgdl=statistics.mean(sum(c.values()) for _,c in lexical)
    gateway.phase='query';rows=[]
    def relevant(node,q):
        version=node.get('version_id');item=items.get(version)
        if version not in q['expected_files'] or not item:return False
        gold=next(g for g in item['gold'] if g['fact']==q['fact'])
        return anchor_matches(gold['anchor'],node.get('anchor',{})) and norm(gold['text']) in norm(node.get('text',node.get('body','')))
    def score_exact(query,counter):
        dl=sum(counter.values());score=0.
        for t in set(tokens(query)):
            tf=counter[t]
            if tf:score+=math.log(1+(len(nodes)-df[t]+.5)/(df[t]+.5))*tf*2.2/(tf+1.2*(.25+.75*dl/avgdl))
        return score
    def metrics(hits,q):
        recovered={h['version_id'] for h in hits if relevant(h,q)}
        citations=0
        for h in hits:
            block=allblocks.get(h['version_id'],{}).get(h.get('anchor',{}).get('block_id'))
            if block and norm(h['text']) in norm(block['text']) and all(h['anchor'].get(k)==v for k,v in block['anchor'].items()):citations+=1
        gains=[];seen=set();first=0
        for rank,h in enumerate(hits,1):
            rel=relevant(h,q)
            if rel and not first:first=rank
            gain=int(rel and h['version_id'] not in seen);gains.append(gain)
            if rel:seen.add(h['version_id'])
        ideal=sum(1/math.log2(rank+1) for rank in range(1,min(8,len(q['expected_files']))+1))
        return {'recall_at_8':len(recovered)/len(q['expected_files']),'hit_at_8':float(bool(recovered)),'reciprocal_rank_at_8':1/first if first else 0,'unique_source_ndcg_at_8':sum(g/math.log2(rank+1) for rank,g in enumerate(gains,1))/ideal if ideal else 0,'relevant_files':sorted(recovered),'expected_files':q['expected_files'],'citation_integrity':citations/len(hits) if hits else 0,'relevant_hit_precision':sum(relevant(h,q) for h in hits)/len(hits) if hits else 0}

    for q in manifest['queries']:
        begin=time.perf_counter();ranked=sorted(((score_exact(q['query'],c),n) for n,c in lexical),key=lambda v:(-v[0],v[1]['node_id']))[:8]
        exact=[{'id':n['node_id'],'version_id':n['version_id'],'text':n['body'],'anchor':json.loads(n['anchor'])} for score,n in ranked if score>0];exact_ms=(time.perf_counter()-begin)*1000
        begin=time.perf_counter();found=await rpc('search',{**params,'query':q['query'],'embedding_version':model_info['version'],'limit':8});hybrid_ms=(time.perf_counter()-begin)*1000
        hits=found['hits'];resolved=await rpc('resolve',{**params,'node_ids':[h['id'] for h in hits]}) if hits else {'evidence':[]}
        representable={n['version_id'] for n in nodes if relevant({**n,'anchor':json.loads(n['anchor'])},q)}
        row={'id':q['id'],'query':q['query'],'language':q['language'],'diagnostics':{'expected_count':len(q['expected_files']),'theoretical_recall_ceiling':min(8,len(q['expected_files']))/len(q['expected_files']),'single_chunk_representable_files':sorted(representable),'extraction_chunking_recall_ceiling':len(representable)/len(q['expected_files'])},'exact':{**metrics(exact,q),'latency_ms':exact_ms},'hybrid':{**metrics(hits,q),'latency_ms':hybrid_ms},'resolved_count':len(resolved['evidence']),'hits':hits,'exact_hits':exact};rows.append(row)
        dump(work/('reranked-query-checkpoints' if reranker_info else 'query-checkpoints')/(q['id']+'.json'),row)
        print(json.dumps({'query':q['id'],'exact_recall':row['exact']['recall_at_8'],'hybrid_recall':row['hybrid']['recall_at_8'],'hybrid_ms':hybrid_ms}),flush=True)
    # Structural authorization checks use real engine paths but injected authority.
    checks={'knowledge_rpc_internal_token_denied':True,'twenty_files_two_representations':len(multi)==40 and multi_index['status']=='staged'}
    multi_search=await rpc('search',{'collection_id':'benchmark','generation':'generation2','query':manifest['queries'][0]['query'],'embedding_version':model_info['version'],'limit':8})
    checks['multiple_representations_search']=bool(multi_search['hits']) and all(h['representation_id'].endswith(('_part0','_part1')) for h in multi_search['hits'])
    await rpc('delete_generation',{'collection_id':'benchmark','generation':'generation2'})
    checks['ungranted_user_empty']=not (await engine.search(bob,'benchmark','generation1','policy',embedding_version=model_info['version']))['hits']
    revoked=next(h['version_id'] for row in rows for h in row['hits']);allowed.remove(revoked)
    hitids=[h['id'] for row in rows for h in row['hits'] if h['version_id']==revoked][:100]
    checks['revoked_source_not_resolved']=not (await engine.resolve(alice,'benchmark','generation1',hitids))['evidence'] if hitids else None
    try:await engine.search(Identity('other_tenant','alice','workspace'),'benchmark','generation1','policy',embedding_version=model_info['version'])
    except NotFoundError:checks['cross_tenant_denied']=True
    else:checks['cross_tenant_denied']=False
    try:await engine.search(Identity('benchmark','alice','other_workspace'),'benchmark','generation1','policy',embedding_version=model_info['version'])
    except NotFoundError:checks['cross_workspace_denied']=True
    else:checks['cross_workspace_denied']=False
    deleted=await engine.delete_generation(alice,'benchmark','generation1');checks['delete_cleans_vectors']=await vectors.bind(alice.tenant_context()).count('document_chunks')==0
    try:await engine.search(alice,'benchmark','generation1','policy',embedding_version=model_info['version'])
    except ConflictError:checks['deleted_generation_denied']=True
    else:checks['deleted_generation_denied']=False
    await channel.close();await server.stop(0);await db.aclose()
    dump(work/('retrieval-reranked-summary.json' if reranker_info else 'retrieval-summary.json'),{'environment':environment(),'model':model_info,'reranker':reranker_info,'exact_baseline':'Un-hashed BM25 lexical matching, k1=1.2 b=0.75, same chunks and top8; not dense exact-neighbor search','implementation':'Actual Knowledge gRPC index/search/resolve + LlamaIndex RetrievalEngine + GatewayEmbedder request/response adapter + CPU E5 + Runtime InMemoryVectorStore + SQLite ledger. '+('Dense40/sparse40 union, up to80 candidates, GatewayReranker with fixed local cross-encoder.' if reranker_info else 'Hybrid RRF60, no learned reranker.')+' No network ModelGateway server, production vector backend or LLM answer generation.','indexed_documents':len(docs),'index':index,'index_seconds':index_seconds,'queries':rows,'authority_checks':checks,'deleted':deleted,'cost':{'paid_api_requests':0,'model_calls':gateway.calls,'model_input_tokens':gateway.input_tokens,'model_cpu_wall_seconds':gateway.seconds,'reranker_calls':gateway.score_calls,'reranker_pairs':gateway.score_pairs,'reranker_input_tokens':gateway.score_tokens,'reranker_cpu_wall_seconds':gateway.score_seconds,'cpu_quota':4,'memory_limit_gib':8},'limits':['Synthetic relevance judgments, not human annotated.','Model outputs are local CPU inference, not remote hosted-model quality guarantees.','Citation integrity is structural provenance; relevant_hit_precision measures actual query relevance separately.']})


def comparison(work):
    """Re-score preserved raw rankings without changing their frozen relevance labels."""
    import html
    manifest=load(work/'manifest.json');items={f['id']:f for f in manifest['files']};questions={q['id']:q for q in manifest['queries']}
    baseline=load(work/'retrieval-summary.json')
    improved=load(work/'retrieval-reranked-summary.json') if (work/'retrieval-reranked-summary.json').exists() else None
    def relevance(hit,q):
        item=items.get(hit.get('version_id'))
        if item is None or item['id'] not in q['expected_files']:return False
        gold=next(g for g in item['gold'] if g['fact']==q['fact'])
        return anchor_matches(gold['anchor'],hit.get('anchor',{})) and norm(gold['text']) in norm(hit.get('text',''))
    def metric(hits,q):
        recovered=set();gains=[];first=0
        for rank,hit in enumerate(hits,1):
            relevant=relevance(hit,q)
            if relevant and not first:first=rank
            gains.append(int(relevant and hit['version_id'] not in recovered))
            if relevant:recovered.add(hit['version_id'])
        ideal=sum(1/math.log2(rank+1) for rank in range(1,min(8,len(q['expected_files']))+1))
        valid=0
        for hit in hits:
            item=items.get(hit['version_id']);parsed=load(work/'parse'/(item['id']+'.json'))['result'] if item else {}
            block=next((b for b in parsed.get('blocks',[]) if b['id']==hit.get('anchor',{}).get('block_id')),None)
            valid+=bool(block and norm(hit['text']) in norm(block['text']) and all(hit['anchor'].get(k)==v for k,v in block['anchor'].items()))
        return {'recall_at_8':len(recovered)/len(q['expected_files']),'hit_at_8':float(bool(first)),'mrr_at_8':1/first if first else 0,'unique_source_ndcg_at_8':sum(g/math.log2(rank+1) for rank,g in enumerate(gains,1))/ideal,'citation_integrity':valid/len(hits) if hits else 0,'relevant_hit_precision':sum(relevance(h,q) for h in hits)/len(hits) if hits else 0,'relevant_files':sorted(recovered)}
    indexed={q['id']:q for q in improved['queries']} if improved else {}
    rows=[];examples=[]
    for raw in baseline['queries']:
        q=questions[raw['id']];row={'id':q['id'],'query':q['query'],'language':q['language'],'expected_files':q['expected_files'],'expected_count':len(q['expected_files']),'theoretical_recall_ceiling':min(8,len(q['expected_files']))/len(q['expected_files'])}
        for mode,hits,timing in [('exact',raw['exact_hits'],raw['exact']['latency_ms']),('hybrid',raw['hits'],raw['hybrid']['latency_ms'])]+([('reranked',indexed[q['id']]['hits'],indexed[q['id']]['hybrid']['latency_ms'])] if improved else []):
            row[mode]={**metric(hits,q),'latency_ms':timing}
        if improved:row['single_chunk_representable_files']=indexed[q['id']]['diagnostics']['single_chunk_representable_files']
        rows.append(row)
        if len(examples)<6 and row['hybrid']['recall_at_8']<1:
            examples.append({'id':q['id'],'query':q['query'],'expected':[{'file':fid,'fact':q['fact'],'text':next(g['text'] for g in items[fid]['gold'] if g['fact']==q['fact'])} for fid in q['expected_files']],'hybrid_top8':raw['hits'],'reranked_top8':indexed[q['id']]['hits'] if improved else []})
    modes=('exact','hybrid','reranked') if improved else ('exact','hybrid')
    aggregates={}
    for mode in modes:
        values=[r[mode] for r in rows]
        aggregate={key:statistics.mean(v[key] for v in values) for key in ('recall_at_8','hit_at_8','mrr_at_8','unique_source_ndcg_at_8','citation_integrity','relevant_hit_precision')}
        aggregate.update(p50_ms=percentile([v['latency_ms'] for v in values],.5),p95_ms=percentile([v['latency_ms'] for v in values],.95))
        aggregate['by_language']={lang:{key:statistics.mean(r[mode][key] for r in rows if r['language']==lang) for key in ('recall_at_8','hit_at_8','mrr_at_8')} for lang in ('en','zh')}
        aggregate['by_source_format']={family:sum(sum(items[f]['family']==family for f in r[mode]['relevant_files']) for r in rows)/sum(sum(items[f]['family']==family for f in r['expected_files']) for r in rows) for family in sorted({items[f]['family'] for r in rows for f in r['expected_files']})}
        aggregates[mode]=aggregate
    missing=[]
    for item in manifest['files']:
        record=load(work/'parse'/(item['id']+'.json'))
        for check in record['evaluation']['checks']:
            if check['text_found'] and check['anchor_found']:continue
            gold=next(g for g in item['gold'] if g['fact']==check['fact'])
            missing.append({'file':item['id'],'path':item['path'],'family':item['family'],'variant':item['structural_variant'],'language':item['language'],'expected':gold,'actual_blocks':[b for b in record['result'].get('blocks',[]) if anchor_matches(gold['anchor'],b.get('anchor',{}))]})
    result={'manifest_sha256':digest(work/'manifest.json'),'query_count':len(rows),'golden_labels_changed':False,'human_reviewed_anchors':0,'metrics':aggregates,'queries':rows,'ranking_failure_examples':examples,'parse_misses':missing,'baseline_cost':baseline['cost'],'reranked_cost':improved['cost'] if improved else None,'reranker':improved.get('reranker') if improved else None,'limitations':['Every expected set has at most eight files, so Recall@8 is not capped by the denominator.','Relevance requires full frozen clause in a single chunk at the matching anchor; OCR splits/loss remain in the denominator.','nDCG rewards each relevant source only once; duplicate chunks from one source cannot inflate it.','These labels are generated ground truth; no human annotation gate is claimed.']}
    dump(work/'comparison.json',result)
    with (work/'retrieval-comparison.csv').open('w',newline='') as file:
        writer=csv.writer(file);writer.writerow(['query_id','language','query','mode','expected_sources','recall_at_8','hit_at_8','mrr_at_8','unique_source_ndcg_at_8','citation_integrity','relevant_hit_precision','latency_ms'])
        for row in rows:
            for mode in modes:writer.writerow([row['id'],row['language'],row['query'],mode,row['expected_count'],*[row[mode][k] for k in ('recall_at_8','hit_at_8','mrr_at_8','unique_source_ndcg_at_8','citation_integrity','relevant_hit_precision','latency_ms')]])
    body=['<!doctype html><meta charset="utf-8"><title>File Intelligence synthetic review</title><style>body{font:15px/1.6 system-ui;max-width:1120px;margin:40px auto;padding:0 20px;background:#fafafa;color:#222}table{border-collapse:collapse;width:100%}td,th{padding:8px;border-bottom:1px solid #ddd;text-align:left}details{padding:16px;margin:12px 0;background:white}pre{white-space:pre-wrap}small{color:#666}</style><h1>120-file / 690-anchor / 100-query review</h1><p><strong>Synthetic generated gold. Human-reviewed anchors: 0. This report does not pass the manual annotation release gate.</strong></p><p>Frozen manifest: <code>'+digest(work/'manifest.json')+'</code></p><h2>Preserved baseline and one bounded reranking candidate</h2><table><tr><th>Mode<th>Recall@8<th>Hit@8<th>MRR@8<th>nDCG@8<th>Relevant precision<th>Citation integrity<th>p95 ms']
    for mode,value in aggregates.items():body.append('<tr><th>'+mode+''.join('<td>'+f'{value[key]:.4f}' for key in ('recall_at_8','hit_at_8','mrr_at_8','unique_source_ndcg_at_8','relevant_hit_precision','citation_integrity','p95_ms')))
    body.append('</table><h2>Source anchor review (not reviewed)</h2><p>Extract corpus.tar.gz next to this HTML. The links open byte-identical source files. Record human reviews in human-anchor-review.csv.</p>')
    for item in manifest['files']:
        if not item['gold']:continue
        body.append('<details><summary>'+html.escape(item['id']+' · '+item['family']+' · '+item['structural_variant'])+'</summary><a href="inputs/'+html.escape(item['path'],quote=True)+'">Open source</a><small> SHA256 '+item['sha256']+'</small>')
        for gold in item['gold']:body.append('<p><code>'+html.escape(json.dumps(gold['anchor'],ensure_ascii=False))+'</code><br>'+html.escape(gold['text'])+'</p>')
        body.append('</details>')
    body.append('<h2>Every query and source coverage</h2>')
    for row in rows:body.append('<details><summary>'+html.escape(row['id']+' '+row['query'])+'</summary><pre>'+html.escape(json.dumps(row,ensure_ascii=False,indent=2))+'</pre></details>')
    body.append('<h2>Unchanged-gold OCR misses and ranking examples</h2><pre>'+html.escape(json.dumps({'parse_misses':missing,'ranking_examples':examples},ensure_ascii=False,indent=2))+'</pre>')
    (work/'review.html').write_text(''.join(body))
    return result


def report(work):
    verify_frozen(work)
    manifest=load(work/'manifest.json')
    if not (work/'human-anchor-review.csv').exists():
        with (work/'human-anchor-review.csv').open('w',newline='') as review:
            writer=csv.writer(review);writer.writerow(['file','source_sha256','fact','expected_anchor','expected_text','review_status','reviewer','reviewed_at'])
            for item in manifest['files']:
                for gold in item['gold']:writer.writerow([item['path'],item['sha256'],gold['fact'],json.dumps(gold['anchor'],ensure_ascii=False),gold['text'],'pending','',''])
    parsed=load(work/'parse-summary.json');retrieval=load(work/'retrieval-summary.json') if (work/'retrieval-summary.json').exists() else None
    families={}
    for family in sorted({f['family'] for f in manifest['files']}):
        rows=[r for r in parsed['files'] if r['family']==family];checks=[c for r in rows for c in r['checks']];readable=[r for r in rows if r['expected']=='readable']
        families[family]={'files':len(rows),'statuses':dict(collections.Counter(r['status'] for r in rows)),'golden_anchors':len(checks),'exact_text_and_anchor':sum(c['text_found'] and c['anchor_found'] for c in checks),'p50_seconds':percentile([r['elapsed_seconds'] for r in readable],.5),'p95_seconds':percentile([r['elapsed_seconds'] for r in readable],.95),'unchanged':all(r['original_unchanged'] for r in rows),'by_variant':{variant:{'files':sum(r['variant']==variant for r in rows),'targets':sum(len(r['checks']) for r in rows if r['variant']==variant),'exact':sum(c['text_found'] and c['anchor_found'] for r in rows if r['variant']==variant for c in r['checks']),'ocr_low_confidence_blocks':sum(r.get('ocr',{}).get('low_confidence_blocks',0) for r in rows if r['variant']==variant)} for variant in sorted({r['variant'] for r in rows})},'by_language':{language:{'targets':sum(len(r['checks']) for r in rows if r['language']==language),'exact':sum(c['text_found'] and c['anchor_found'] for r in rows if r['language']==language for c in r['checks'])} for language in sorted({r['language'] for r in rows})}}
    summary={'provenance':manifest['provenance'],'human_review':manifest['human_review'],'parser_environment':parsed.get('environment'),'notebook_read_only':parsed.get('notebook_read_only'),'files':len(manifest['files']),'parsed_files':len(parsed['files']),'golden_anchors':sum(len(f['gold']) for f in manifest['files']),'families':families,'exact_100k_csv':load(work/'exact-compute.json')['passed'] if (work/'exact-compute.json').exists() else None,'limitations':['Generated source truth independently checked for text/OOXML/PDF where possible; raster source/reading order still requires human visual review.','Page/cell/paragraph/shape locations and normalized bbox bounds checked; no human geometric bbox IoU measurement.','A synthetic corpus is not the fixed human-labeled release set; the >=500 manually reviewed anchors gate is not met by this run.','Engine CLI and injected RetrievalEngine authority do not prove all HTTP/Temporal/production-backend lifecycle gates.']}
    if retrieval:
        summary['retrieval']={'documents':retrieval['indexed_documents'],'queries':len(retrieval['queries']),'model':retrieval['model'],'index_seconds':retrieval['index_seconds'],'authority_checks':retrieval['authority_checks'],'cost':retrieval['cost']}
        file_families={f['id']:f['family'] for f in manifest['files']}
        summary['retrieval']['by_source_format']={}
        for family in sorted(families):
            denominator=sum(sum(file_families[f]==family for f in q['exact']['expected_files']) for q in retrieval['queries'])
            if denominator:
                summary['retrieval']['by_source_format'][family]={mode:sum(sum(file_families[f]==family for f in q[mode]['relevant_files']) for q in retrieval['queries'])/denominator for mode in ('exact','hybrid')}
        for mode in ('exact','hybrid'):
            vals=[q[mode] for q in retrieval['queries']]
            summary['retrieval'][mode]={'mean_recall_at_8':statistics.mean(v['recall_at_8'] for v in vals),'mean_relevant_hit_precision':statistics.mean(v['relevant_hit_precision'] for v in vals),'mean_citation_integrity':statistics.mean(v['citation_integrity'] for v in vals),'p50_ms':percentile([v['latency_ms'] for v in vals],.5),'p95_ms':percentile([v['latency_ms'] for v in vals],.95)}
    if retrieval:
        compared=comparison(work)
        summary['fixed_gold_comparison']=compared['metrics']
    dump(work/'summary.json',summary)
    print(json.dumps(summary,ensure_ascii=False,indent=2))


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('command',choices=['generate','parse','retrieve','report']);ap.add_argument('--work',type=Path,required=True);ap.add_argument('--only',help='Comma-separated parse families, supports resumable batches');ap.add_argument('--model-dir');ap.add_argument('--reranker-dir')
    args=ap.parse_args();args.work=args.work.resolve();args.work.mkdir(parents=True,exist_ok=True)
    if args.command=='generate':generate(args.work)
    elif args.command=='parse':parse(args.work,args.only)
    elif args.command=='retrieve':asyncio.run(retrieve(args.work,args.model_dir,args.reranker_dir))
    else:report(args.work)

if __name__=='__main__':main()
