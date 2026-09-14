import hashlib, json, time
from pathlib import Path
import httpx

# Cosmetic OpenAPI fields the platform may edit without any behavioural change.
# Hashing them froze all writes for hours when a single endpoint's prose was
# extended (see contract-freeze incident). We hash only the structural surface.
#   - now/now_utc: server clock, always varies.
#   - example/examples: pure annotation keywords; drop unconditionally.
#   - description/summary: OpenAPI annotations are ALWAYS strings, whereas a
#     request-schema property literally named "description" is a dict -> we drop
#     these two ONLY when the value is a str, so real schema properties survive.
_COSMETIC_ANY={'now','now_utc','example','examples'}
_COSMETIC_STR={'description','summary'}
def _structural(value):
    if isinstance(value,dict):
        out={}
        for k,v in value.items():
            if k in _COSMETIC_ANY: continue
            if k in _COSMETIC_STR and isinstance(v,str): continue
            out[k]=_structural(v)
        return out
    if isinstance(value,list): return [_structural(v) for v in value]
    return value
def contract_hash(value):
    return hashlib.sha256(json.dumps(_structural(value),sort_keys=True,separators=(',',':')).encode()).hexdigest()

class Client:
    def __init__(self,settings,db,transport=None):
        self.settings=settings; self.db=db
        self.session=httpx.Client(base_url=settings.api_base,transport=transport,timeout=30,follow_redirects=False,headers={'Authorization':f'Bearer {settings.api_key}'} if settings.api_key else {})
        self.db.secrets.extend([settings.api_key,settings.dashboard_password])
    def _path(self,path):
        if not path.startswith('/') or path.startswith('//') or '\\' in path or '#' in path: raise ValueError('Relative API path required')
        return path
    def _request(self,method,path,**kwargs):
        for attempt in range(3 if method=='GET' else 1):
            started=time.monotonic()
            try:
                response=self.session.request(method,self._path(path),**kwargs)
                try: body=response.json()
                except ValueError: body={'non_json':True}
                # Do NOT persist full response bodies: large ones (e.g.
                # /openapi.json, listing walks ~800KB each) bloated the events
                # table to ~760MB and made the dashboard take 30s/page. Keep the
                # size, and only a truncated body for error responses (the useful
                # diagnostic case). Nothing reads api_calls['response'] downstream.
                entry={'method':method,'path':path,'payload':kwargs.get('json',kwargs.get('params')),'status':response.status_code,'duration':time.monotonic()-started,'resp_bytes':len(response.content)}
                if response.status_code>=400: entry['response']=str(body)[:1000]
                self.db.log('api_calls',entry)
                if method=='GET' and response.status_code in {429,502,503,504} and attempt<2:
                    try: delay=min(2,max(0,float(response.headers.get('Retry-After',0.2*(attempt+1)))))
                    except ValueError: delay=0.2
                    time.sleep(delay); continue
                return response
            except httpx.TransportError as exc:
                self.db.log('api_calls',{'method':method,'path':path,'error_type':type(exc).__name__,'duration':time.monotonic()-started})
                if method=='GET' and attempt<2:
                    time.sleep(0.2*(attempt+1)); continue
                raise
    def get(self,path,params=None):
        path=self._path(path); key='etag:'+path+json.dumps(params,sort_keys=True)
        etag=self.db.get_setting(key)
        response=self._request('GET',path,params=params,headers={'If-None-Match':etag} if etag else {})
        if response.status_code==304: return self.db.get_setting(key+':body')
        response.raise_for_status(); data=response.json()
        if response.headers.get('etag'):
            self.db.set_setting(key,response.headers['etag'])
            self.db.set_setting(key+':body',data)
        return data
    def check_contract(self):
        try:
            baseline=json.loads((Path(__file__).resolve().parents[1]/'contracts/openapi.json').read_text())
            response=self._request('GET','/openapi.json'); response.raise_for_status()
            ok=contract_hash(response.json())==contract_hash(baseline)
        except Exception: ok=False
        self.db.set_setting('contract_ok',ok)
        if not ok: self.db.log('contract',{'status':'writes_blocked'})
        return ok
    def post(self,path,payload):
        if not self.settings.api_key: raise RuntimeError('No identity')
        if not self.check_contract(): raise RuntimeError('Contract unavailable or changed')
        response=self._request('POST',path,json=payload)
        response.raise_for_status()
        return response.json()
    def close(self): self.session.close()
