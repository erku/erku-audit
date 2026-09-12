import concurrent.futures
from datetime import datetime, timezone
from f916.db import Database
from f916.config import Settings
from f916.models import Intent
from f916.actions import Executor

def test_atomic_daily_limit(tmp_path):
    db=Database(tmp_path/'state.db'); db.initialize()
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        results=list(pool.map(lambda n: db.reserve_action({'action':'post','title':str(n)}, now=1789250000),range(12)))
    assert sum(results)==1
    assert db.reserve_action({'action':'post','title':'tomorrow'},now=1789336400)

def test_durable_dedup_and_queue(tmp_path):
    p=tmp_path/'state.db'; db=Database(p); db.initialize()
    intent={'action':'vote','post_id':4}
    assert db.reserve_action(intent)
    assert not Database(p).reserve_action(intent)
    q=db.queue(intent,'review'); db.resolve_queue(q,'approved')
    assert db.claim_queue(q)
    assert not db.claim_queue(q)

def test_modes_and_uncertain_no_retry(tmp_path):
    db=Database(tmp_path/'s.db'); db.initialize()
    class API:
        calls=0
        def check_contract(self): return True
        def get(self,*a,**k): return {'post':{'author':'other'}}
        def post(self,*a):
            self.calls+=1
            raise TimeoutError('secret-canary')
    api=API(); s=Settings(api_key='secret-canary',handle='self',mode='approve')
    ex=Executor(s,db,api)
    assert ex.dispatch(Intent(action='comment',post_id=1,body='hello'))['status']=='queued'
    db.set_setting('mode','off')
    assert ex.dispatch(Intent(action='vote',post_id=1),approved=True)['status']=='blocked'
    db.set_setting('mode','auto')
    i=Intent(action='vote',post_id=1)
    assert ex.dispatch(i)['status']=='uncertain'
    assert ex.dispatch(i)['status']=='blocked'
    assert api.calls==1
    assert 'secret-canary' not in str(db.events())

def test_rolling_and_utc_limits(tmp_path):
    db=Database(tmp_path/'s.db'); db.initialize(); t=1789257599
    for n in range(10): assert db.reserve_action({'action':'submit','listing_id':n},now=t)
    assert not db.reserve_action({'action':'submit','listing_id':90},now=t+1)
    assert db.reserve_action({'action':'submit','listing_id':91},now=t+86401)
    assert db.reserve_action({'action':'post','title':'aaa'},now=t)
    assert db.reserve_action({'action':'post','title':'bbb'},now=(t//86400+1)*86400)

def test_queue_edit_only_pending(tmp_path):
    db=Database(tmp_path/'s.db'); db.initialize()
    q=db.queue({'action':'comment','post_id':1,'body':'a'},'review')
    assert db.edit_queue(q,{'action':'comment','post_id':1,'body':'b'})
    assert db.resolve_queue(q,'approved')
    assert not db.edit_queue(q,{'action':'noop'})
    assert not db.resolve_queue(q,'approved')

def test_contract_and_post_no_retry(tmp_path):
    import httpx,json
    from pathlib import Path
    from f916.client import Client
    baseline=json.loads(Path('contracts/openapi.json').read_text()); calls=[]
    def handler(request):
        calls.append(request.method)
        if request.url.path=='/openapi.json': return httpx.Response(200,json=baseline|{'now':99})
        raise httpx.ReadTimeout('uncertain')
    db=Database(tmp_path/'s.db'); db.initialize()
    c=Client(Settings(api_key='canary'),db,transport=httpx.MockTransport(handler))
    assert c.check_contract()
    import pytest
    with pytest.raises(httpx.ReadTimeout): c.post('/api/post',{})
    assert calls.count('POST')==1
    c.session.close()
    c=Client(Settings(api_key='canary'),db,transport=httpx.MockTransport(lambda r:httpx.Response(503)))
    assert not c.check_contract()
    with pytest.raises(RuntimeError): c.post('/api/post',{})
    c.session.close()
    c=Client(Settings(),db,transport=httpx.MockTransport(lambda r:httpx.Response(200,json=baseline|{'openapi':'changed'})))
    assert not c.check_contract()
    c.close()

def test_registration_durable_and_signed(tmp_path):
    import httpx,json,base64
    from scripts.register import register
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    calls=[]
    def handler(request):
        assert (tmp_path/'identity-ed25519.pem').exists()
        assert (tmp_path/'registration-attempt.json').exists()
        p=json.loads(request.content); dec=lambda s:base64.urlsafe_b64decode(s+'='*(-len(s)%4))
        Ed25519PublicKey.from_public_bytes(dec(p['public_key'])).verify(dec(p['signature']),f"1f916.key-bind.v1:{p['handle']}:{p['public_key']}".encode())
        calls.append(1); return httpx.Response(200,json={'secret':'super-secret'})
    out=register(tmp_path,'test-agent','test-model',transport=httpx.MockTransport(handler))
    assert 'super-secret' not in str(out)
    assert (tmp_path/'citizen-secret.txt').read_text().strip()=='super-secret'
    import pytest
    with pytest.raises(FileExistsError): register(tmp_path,'test-agent','test-model',transport=httpx.MockTransport(handler))
    assert len(calls)==1

def test_self_vote_is_blocked_and_auto_submission_is_sent(tmp_path):
    db=Database(tmp_path/'s.db'); db.initialize()
    class API:
        def get(self,*a,**k): return {'post':{'author':'self'}}
        def check_contract(self): return True
        def post(self,*a,**k): return {'id': 7}
    ex=Executor(Settings(api_key='key',handle='self',mode='auto'),db,API())
    assert ex.dispatch(Intent(action='vote',post_id=1))['status']=='blocked'
    assert ex.dispatch(Intent(action='submit',listing_id=1,artifact='hash:123'))['status']=='sent'
    db.log('secret',{'api_key':'key','body':'contains key'})
    assert db.events()[0]['data']=={'api_key':'[REDACTED]','body':'contains [REDACTED]'}

def test_executor_enforces_hard_content_invariants(tmp_path):
    db=Database(tmp_path/'s.db'); db.initialize()
    class API: pass
    ex=Executor(Settings(api_key='key',handle='self',mode='auto'),db,API())
    result=ex.dispatch(Intent(action='comment',post_id=2,body='Donate to my wallet'))
    assert result['status']=='blocked'
    assert result['reason']=='invariant'

