"""Automatically route real awards to an already-proved Base EOA."""
from __future__ import annotations

import base64
from pathlib import Path
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import load_pem_private_key


def _b64(value): return base64.urlsafe_b64encode(value).decode().rstrip('=')


def build_binding_payload(preimage, settings):
    fields=('amount_atomic','chain_id','token','expiry','preimage')
    if not isinstance(preimage,dict) or any(key not in preimage for key in fields): raise ValueError('Incomplete payout preimage')
    address=settings.payout_address.lower(); row=preimage.get('amount_filled_from')
    expected=f"1f916.payout.v1:{settings.handle}:{row}:{preimage['amount_atomic']}:{preimage['chain_id']}:{str(preimage['token']).lower()}:{address}:{preimage['expiry']}"
    if preimage['preimage'] != expected: raise ValueError('Payout preimage mismatch')
    key=load_pem_private_key((Path(settings.data_dir)/'identity-ed25519.pem').read_bytes(),password=None)
    public=_b64(key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw))
    return {'version':'1f916.payout.v1','handle':settings.handle,'row':row,
            'amount_atomic':str(preimage['amount_atomic']),'chain_id':preimage['chain_id'],
            'token':str(preimage['token']).lower(),'address':address,'expiry':preimage['expiry'],
            'citizen_public_key':public,'citizen_signature':_b64(key.sign(expected.encode())),
            'preimage':expected}


class PayoutManager:
    def __init__(self,settings,db,client): self.settings,self.db,self.client=settings,db,client

    def _wallet_live(self):
        result=self.client.get('/api/payout-wallets')
        rows=result if isinstance(result,list) else result.get('wallets',result.get('addresses',[])) if isinstance(result,dict) else []
        return any(isinstance(row,dict) and str(row.get('address','')).lower()==self.settings.payout_address.lower()
                   and (row.get('live') is True or row.get('status')=='live') for row in rows)

    def reconcile(self, row):
        result=self.client.get('/api/payouts',params={'docket':row})
        rows=result if isinstance(result,list) else result.get('bindings',result.get('payouts',[])) if isinstance(result,dict) else []
        return next((item for item in rows if isinstance(item,dict) and item.get('handle',item.get('payee'))==self.settings.handle),None)

    def ensure_for_listing(self, listing):
        row=f"listing-{int(listing['listing_id'])}"
        existing=self.reconcile(row)
        if existing: return {'status':'exists','row':row,'binding':existing}
        if self.db.get_setting('payout_binding:'+row) in ('attempted','uncertain'): return {'status':'uncertain','row':row}
        if not self._wallet_live(): return {'status':'blocked','reason':'no_live_proved_wallet','row':row}
        expiry=min(int(time.time())+2591500,int(listing['expiry']))
        if expiry <= int(time.time())+300: return {'status':'blocked','reason':'listing_expired','row':row}
        prepared=self.client.get('/api/payout-bindings/preimage',params={
            'handle':self.settings.handle,'row':row,'address':self.settings.payout_address,'expiry':expiry})
        payload=build_binding_payload(prepared,self.settings)
        self.db.set_setting('payout_binding:'+row,'attempted')
        try: result=self.client.post('/api/payout-bindings',payload)
        except Exception:
            self.db.set_setting('payout_binding:'+row,'uncertain'); raise
        self.db.set_setting('payout_binding:'+row,'bound')
        self.db.log('payout_binding',{'status':'bound','row':row,'result':result})
        return {'status':'bound','row':row,'result':result}

    def scan_awards(self, listings):
        results=[]
        for summary in listings if isinstance(listings,list) else []:
            if not isinstance(summary,dict) or not summary.get('awards'): continue
            detail=self.client.get(f"/api/listings/{int(summary['listing_id'])}")
            submissions={item.get('id'):item for item in detail.get('submissions',[]) if isinstance(item,dict)}
            for award in detail.get('awards',[]):
                own=submissions.get(award.get('submission_id'),{}).get('handle')==self.settings.handle
                needs=award.get('state') in {'awarded','payable','overdue_unpaid'} and not award.get('ready_payout_address')
                if own and needs:
                    results.append(self.ensure_for_listing(detail)); break
        return results
