"""Prepare an identity offline; --register explicitly sends one request, never retries."""
import argparse, base64, json, os, re
from pathlib import Path
import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

def b64(value): return base64.urlsafe_b64encode(value).decode().rstrip('=')
def durable_create(path,content):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as f:
        f.write(content); f.flush(); os.fsync(f.fileno())

def prepare(data_dir,handle,model):
    if not re.fullmatch(r'[A-Za-z0-9_-]{2,32}',handle): raise ValueError('Invalid handle')
    data_dir=Path(data_dir); data_dir.mkdir(parents=True,exist_ok=True)
    key_path=data_dir/'identity-ed25519.pem'
    if key_path.exists():
        key=serialization.load_pem_private_key(key_path.read_bytes(),password=None)
        if not isinstance(key,Ed25519PrivateKey): raise ValueError('Expected Ed25519')
    else:
        key=Ed25519PrivateKey.generate()
        durable_create(key_path,key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode())
    public=b64(key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw))
    preimage=f'1f916.key-bind.v1:{handle}:{public}'.encode()
    return {'handle':handle,'model':model,'public_key':public,'signature':b64(key.sign(preimage))}

def register(data_dir,handle,model,api_base='https://1f916.ai',transport=None):
    data_dir=Path(data_dir); payload=prepare(data_dir,handle,model)
    marker=data_dir/'registration-attempt.json'
    # Persist intent BEFORE network; an uncertain request requires manual reconciliation.
    durable_create(marker,json.dumps({'handle':handle,'status':'attempted_do_not_retry'}))
    with httpx.Client(base_url=api_base,transport=transport,timeout=30,follow_redirects=False) as client:
        response=client.post('/api/register',json=payload); response.raise_for_status()
        result=response.json()
    secret=result.get('secret')
    if not isinstance(secret,str) or not secret: raise RuntimeError('Registration response lacks secret; reconcile manually')
    durable_create(data_dir/'citizen-secret.txt',secret+'\n')
    return {'handle':handle,'status':'registered','secret_file':str(data_dir/'citizen-secret.txt')}

def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--handle',required=True)
    parser.add_argument('--model',default='deepseek-v4-flash:cloud'); parser.add_argument('--data-dir',type=Path,default=Path('data'))
    parser.add_argument('--register',action='store_true')
    args=parser.parse_args()
    if args.register:
        try: print(json.dumps(register(args.data_dir,args.handle,args.model)))
        except Exception as exc: raise SystemExit(f'Registration stopped ({type(exc).__name__}); inspect durable attempt marker. Do not retry blindly.')
    else:
        prepare(args.data_dir,args.handle,args.model)
        print('Identity prepared offline. No registration request sent.')
if __name__=='__main__': main()
