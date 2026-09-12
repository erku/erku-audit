import base64
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from f916.config import Settings
from f916.db import Database
from f916.payout import PayoutManager, build_binding_payload


def settings_with_key(tmp_path):
    key=Ed25519PrivateKey.generate()
    (tmp_path/'identity-ed25519.pem').write_bytes(key.private_bytes(Encoding.PEM,PrivateFormat.PKCS8,NoEncryption()))
    return Settings(data_dir=tmp_path,handle='tester',payout_address='0x'+'12'*20,api_key='key')


def test_build_binding_uses_only_citizen_signature_for_proved_wallet(tmp_path):
    settings=settings_with_key(tmp_path); expiry=1900000000; row='listing-7'; address=settings.payout_address.lower()
    pre=f'1f916.payout.v1:tester:{row}:100000:8453:0x'+('34'*20)+f':{address}:{expiry}'
    payload=build_binding_payload({'amount_atomic':'100000','chain_id':8453,'token':'0x'+'34'*20,
                                   'expiry':expiry,'preimage':pre,'amount_filled_from':row},settings)
    assert 'signature' not in payload
    assert payload['row']==row and payload['citizen_signature']


def test_award_scan_binds_only_own_unrouted_award(tmp_path,monkeypatch):
    settings=settings_with_key(tmp_path); db=Database(tmp_path/'db'); db.initialize(); posts=[]
    expiry=int(time.time())+100000; token='0x'+'34'*20; address=settings.payout_address.lower()
    class API:
        def get(self,path,params=None):
            if path=='/api/listings/7': return {'listing_id':7,'expiry':expiry,
                'submissions':[{'id':2,'handle':'tester'}],
                'awards':[{'submission_id':2,'state':'payable','ready_payout_address':None}]}
            if path=='/api/payouts': return {'bindings':[]}
            if path=='/api/payout-wallets': return {'wallets':[{'address':address,'live':True}]}
            if path=='/api/payout-bindings/preimage':
                row='listing-7'; exp=params['expiry']; pre=f'1f916.payout.v1:tester:{row}:100000:8453:{token}:{address}:{exp}'
                return {'amount_atomic':'100000','chain_id':8453,'token':token,'expiry':exp,'preimage':pre,'amount_filled_from':row}
        def post(self,path,payload): posts.append((path,payload)); return {'id':4}
    result=PayoutManager(settings,db,API()).scan_awards([{'listing_id':7,'awards':1}])
    assert result[0]['status']=='bound'
    assert posts[0][0]=='/api/payout-bindings' and 'signature' not in posts[0][1]
