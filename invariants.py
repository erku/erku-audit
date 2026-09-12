"""Hard action boundaries; policy and model output cannot override these."""
import hashlib
import json
import re
from pathlib import Path

ALLOWED_ACTIONS = frozenset({'post','comment','vote','tag','submit','propose','cadence','porch','noop'})
MANUAL_ACTIONS = frozenset({'propose'})
DAILY_LIMITS = {'post':1, 'comment':20, 'vote':50, 'submit':10}
SECRET_KEYS = frozenset({'api_key','apikey','authorization','password','secret','private_key','citizen_secret','access_token','refresh_token','seed_phrase','mnemonic'})


def redact(value):
    if isinstance(value, dict):
        return {str(k): '[REDACTED]' if str(k).lower().replace('-', '_') in SECRET_KEYS else redact(v) for k,v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r'(?i)(bearer\s+)[\w.\-]+', r'\1[REDACTED]', value)
        value = re.sub(r'(?i)((?:api[_-]?key|secret|password|token|private_key|seed_phrase)[=:\s]+)[^\s&,"<>]+', r'\1[REDACTED]', value)
        value = re.sub(r'\bsk-[A-Za-z0-9_-]{12,}\b', '[REDACTED]', value)
        return value
    return value


def check_intent(intent):
    reasons = []
    if intent.get('action') not in ALLOWED_ACTIONS:
        reasons.append('action_not_allowed')
    body = json.dumps(intent, ensure_ascii=False)
    for name, pattern in (
        ('money_solicitation', r'(?i)\b(donate|donation|send (?:me |us )?(?:money|usdc)|fund my wallet)\b'),
        ('reward_manipulation', r'(?i)(vote for (?:me|us).{0,50}vote for you|trade votes|buy (?:our|my) token|pump.{0,20}token)'),
        ('signing_request', r'(?i)(sign.{0,20}(?:transaction|message)|approve.{0,20}(?:wallet|spend)|connect.{0,10}wallet)'),
    ):
        if re.search(pattern, body): reasons.append(name)
    return reasons


def source_hash():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
