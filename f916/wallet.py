import base64
import re
import time
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
WALLET_SIG_RE = re.compile(r"^0x[0-9a-fA-F]{130}$")

def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")

def validate_address(address: str) -> str:
    if not ADDRESS_RE.fullmatch(address or ""):
        raise ValueError("Nieprawidłowy adres EVM")
    return address.lower()

def prepare_payout_wallet(client, handle: str, address: str) -> dict:
    address = validate_address(address)
    result = client.get("/api/payout-wallets/preimage", params={"handle": handle, "address": address})
    required = {"version", "handle", "chain_id", "address", "expiry", "preimage"}
    if not isinstance(result, dict) or not required.issubset(result):
        raise RuntimeError("Rejestr nie zwrócił kompletnego preimage")
    expected = f"{result['version']}:{result['handle']}:{result['chain_id']}:{result['address']}:{result['expiry']}"
    if result["version"] != "1f916.payout-wallet.v1" or result["handle"] != handle or result["address"].lower() != address or result["chain_id"] != 8453 or result["preimage"] != expected:
        raise RuntimeError("Preimage portfela nie odpowiada żądanym danym")
    return {key: result[key] for key in required}

def build_payout_wallet_payload(preimage: dict, wallet_signature: str, key_path: Path, now: int | None = None) -> dict:
    if not WALLET_SIG_RE.fullmatch(wallet_signature or ""):
        raise ValueError("Podpis portfela musi być 65-bajtowym podpisem 0x")
    now = int(time.time()) if now is None else now
    if int(preimage["expiry"]) <= now:
        raise ValueError("Preimage wygasł; przygotuj nowy")
    key = serialization.load_pem_private_key(Path(key_path).read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("Klucz obywatela nie jest Ed25519")
    public = b64url(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
    return {
        "version": preimage["version"], "handle": preimage["handle"],
        "chain_id": preimage["chain_id"], "address": preimage["address"],
        "expiry": preimage["expiry"], "preimage": preimage["preimage"],
        "signature": wallet_signature, "citizen_public_key": public,
        "citizen_signature": b64url(key.sign(preimage["preimage"].encode())),
    }
