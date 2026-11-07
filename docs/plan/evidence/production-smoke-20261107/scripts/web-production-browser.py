"""Read-only deployed personal configuration UI smoke through native Chromium CDP.
Cookies stay in stdin and CDP pipe; only screenshots and nonsecret UI facts return.
"""
import base64,json,os,select,subprocess,sys,tempfile,time,shutil
from pathlib import Path
request=json.loads(sys.stdin.read());profile=tempfile.mkdtemp(prefix='goal-web-browser-');stderr_path=Path(profile)/'chromium-stderr.log'
r,w=os.pipe();rr,ww=os.pipe()
def child():os.dup2(r,3);os.dup2(ww,4)
with stderr_path.open('wb') as err:
 process=subprocess.Popen(['/usr/bin/chromium','--headless=new','--no-sandbox','--disable-gpu','--no-first-run','--no-default-browser-check','--remote-debugging-pipe','--user-data-dir='+profile],preexec_fn=child,pass_fds=tuple(set((r,ww,3,4))),stdout=subprocess.DEVNULL,stderr=err)
os.close(r);os.close(ww);buffer=b'';serial=0

def call(method,params=None,session=None,timeout=40):
 global serial,buffer
 serial+=1;ident=serial;msg={'id':ident,'method':method,'params':params or {}}
 if session:msg['sessionId']=session
 os.write(w,json.dumps(msg).encode()+b'\0');deadline=time.monotonic()+timeout
 while time.monotonic()<deadline:
  if b'\0' in buffer:
   raw,buffer=buffer.split(b'\0',1)
   if not raw:continue
   got=json.loads(raw)
   if got.get('id')==ident:
    if 'error' in got:raise RuntimeError('CDP '+method+' failed')
    return got.get('result',{})
   continue
  if not select.select([rr],[],[],max(0,deadline-time.monotonic()))[0]:break
  data=os.read(rr,1<<20)
  if not data:raise RuntimeError('browser closed')
  buffer+=data
 raise TimeoutError(method)

def evaluate(expr,session):
 value=call('Runtime.evaluate',{'expression':expr,'awaitPromise':True,'returnByValue':True},session)
 if 'exceptionDetails' in value:raise RuntimeError('browser assertion failed: '+value['exceptionDetails'].get('text','error'))
 return value['result'].get('value')

result={'read_only':True,'model_calls':0,'checks':[]}
try:
 result['browser']=call('Browser.getVersion')
 for who,cookie in request['users'].items():
  context=call('Target.createBrowserContext',{'disposeOnDetach':True})['browserContextId']
  target=call('Target.createTarget',{'url':'about:blank','browserContextId':context})['targetId']
  session=call('Target.attachToTarget',{'targetId':target,'flatten':True})['sessionId']
  try:
   call('Page.enable',session=session);call('Runtime.enable',session=session);call('Network.enable',session=session)
   assert call('Network.setCookie',{'name':'cw_token','value':cookie,'url':request['base'],'secure':True,'httpOnly':True,'sameSite':'Lax'},session)['success']
   call('Emulation.setDeviceMetricsOverride',{'width':1400,'height':1000,'deviceScaleFactor':1,'mobile':False},session)
   call('Page.navigate',{'url':request['base']+'/#/workspaces'},session)
   facts=evaluate("""(async()=>{
    const wait=async f=>{for(let i=0;i<250;i++){const v=f();if(v)return v;await new Promise(r=>setTimeout(r,50));}throw Error('UI timeout');};
    const profile=await wait(()=>document.querySelector('.profile'));profile.click();
    const {t}=await import('/assets/i18n.js');
    const account=[...document.querySelectorAll('.pop-item')].find(x=>x.textContent.includes(t('account.title')));if(!account)throw Error('account entry');account.click();
    (await wait(()=>document.querySelector('.personal-open'))).click();
    await wait(()=>document.querySelector('.personal-instructions'));
    const identity=await fetch('/api/me').then(r=>r.json());
    return {tenant:identity.tenant,user:identity.user,editor:!!document.querySelector('.personal-instructions'),files:!!document.querySelector('.personal-files'),skills:!!document.querySelector('.personal-skills'),mcp:!!document.querySelector('.personal-mcp'),environment:!!document.querySelector('.personal-environment'),save:!!document.querySelector('.personal-save'),instruction_empty:document.querySelector('.personal-instructions').value==='',body_width:document.documentElement.scrollWidth,viewport:innerWidth};
   })()""",session)
   assert facts['user']==who and facts['tenant']==request['tenant']
   assert all(facts[k] for k in ['editor','files','skills','mcp','environment','save'])
   screenshot=call('Page.captureScreenshot',{'format':'png','captureBeyondViewport':False},session)['data']
   result['checks'].append({'user':who,'facts':facts,'screenshot':screenshot})
  finally:call('Target.disposeBrowserContext',{'browserContextId':context})
 result['passed']=True
except Exception as exc:
 result['passed']=False;result['error']={'type':type(exc).__name__,'message':str(exc)}
finally:
 try:call('Browser.close',timeout=5)
 except Exception:pass
 try:process.wait(timeout=10)
 except subprocess.TimeoutExpired:process.kill();process.wait()
 os.close(w);os.close(rr);shutil.rmtree(profile)
 result['cleanup']={'browser_stopped':process.poll() is not None,'profile_removed':not Path(profile).exists()}
 print(json.dumps(result))
