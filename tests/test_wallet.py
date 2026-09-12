import base64
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from f916.wallet import build_payout_wallet_payload, prepare_payout_wallet

class FakeClient:
    def get(self, path, params=None):
        address=params['address']; expiry=2000000000
        return {'version':'1f916.payout-wallet.v1','handle':params['handle'],'chain_id':8453,'address':address,'expiry':expiry,'preimage':f"1f916.payout-wallet.v1:{params['handle']}:8453:{address}:{expiry}"}

def test_prepare_checks_canonical_preimage():
    result=prepare_payout_wallet(FakeClient(),'tester','0x'+'AB'*20)
    assert result['address']=='0x'+'ab'*20

def test_build_signs_exact_preimage_with_citizen_key(tmp_path):
    key=Ed25519PrivateKey.generate(); path=tmp_path/'identity.pem'
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    prepared=prepare_payout_wallet(FakeClient(),'tester','0x'+'ab'*20)
    payload=build_payout_wallet_payload(prepared,'0x'+'11'*65,path,now=1900000000)
    decode=lambda value: base64.urlsafe_b64decode(value+'='*(-len(value)%4))
    key.public_key().verify(decode(payload['citizen_signature']),prepared['preimage'].encode())
    assert payload['signature']=='0x'+'11'*65

def test_invalid_wallet_signature_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        build_payout_wallet_payload({'expiry':2000000000},'bad',tmp_path/'missing',now=1900000000)
