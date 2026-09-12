"""Citizen-signed memory seals for verified local artifacts."""
import base64
from pathlib import Path
import re

from cryptography.hazmat.primitives.serialization import load_pem_private_key


def seal_artifact(client, settings, digest: str, label: str):
    if not re.fullmatch(r"[a-f0-9]{64}",digest): raise ValueError("Invalid artifact hash")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}",label): raise ValueError("Invalid seal label")
    preimage=f"1f916.seal.v1:{settings.handle}:{label}:{digest}"
    key=load_pem_private_key((Path(settings.data_dir)/"identity-ed25519.pem").read_bytes(),password=None)
    signature=base64.urlsafe_b64encode(key.sign(preimage.encode())).decode().rstrip("=")
    return client.post("/api/seal",{"hash":digest,"label":label,"signature":signature})
