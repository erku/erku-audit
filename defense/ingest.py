"""Conservative heuristics, not a proof that content contains no injection."""
import html
import json
import re
import unicodedata
from urllib.parse import urlsplit
from invariants import redact

MAX_CONTENT_BYTES = 256000
PATTERNS = (
    ('instruction_override', r'ignore\s+(?:all\s+)?(?:previous|prior|above|system)\s+(?:instructions|rules|prompts)'),
    ('role_override', r'you are now|system\s*prompt|developer\s*message|\[/?INST\]|<\|(?:system|im_start)\|>'),
    ('authority_spoof', r'(?:maintainer|administrator|admin|official).{0,60}(?:instruct|override|must|secret|sign|wallet)'),
    ('credential_request', r'(?:reveal|send|show|print|provide|paste|upload|share|enter).{0,50}(?:secret|api.?key|private.?key|seed phrase|password|token)'),
    ('wallet_phishing', r'(?:connect|claim|approve|sign).{0,50}(?:wallet|reward|transaction|message|spend)|(?:wallet|transaction).{0,30}(?:approve|sign)'),
    ('boundary_breakout', r'</?untrusted_content|<system>|\b(?:curl|wget).{0,80}(?:secret|token|key)'),
    ('polish_override', r'ignoruj.{0,20}(?:instrukcje|polecenia)|(?:podaj|ujawnij|wyślij).{0,30}(?:klucz|sekret|hasło)|podpisz.{0,20}(?:transakcję|wiadomość)'),
)


def inspect_content(value, *, official_domains=(), extra_patterns=()):
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return {'safe':False, 'reasons':['invalid_input'], 'content':None}
    if len(raw.encode('utf-8')) > MAX_CONTENT_BYTES:
        return {'safe':False, 'reasons':['input_too_large'], 'content':None}
    normalized = unicodedata.normalize('NFKC', raw)
    normalized = ''.join(c for c in normalized if unicodedata.category(c) != 'Cf')
    reasons = [name for name, pattern in PATTERNS if re.search(pattern, normalized, re.I)]
    for pattern in extra_patterns:
        # Additional detector patterns are literal phrases, never untrusted regex.
        if isinstance(pattern,str) and pattern and pattern.casefold() in normalized.casefold(): reasons.append('learned_pattern')
    if re.search(r'\bofficial\b', normalized, re.I):
        for url in re.findall(r'https?://[^\s"<>\\]+', normalized):
            if (urlsplit(url).hostname or '').lower() not in set(official_domains): reasons.append('unverified_official_link')
    return {'safe':not reasons, 'reasons':sorted(set(reasons)), 'content':None if reasons else redact(value)}


def wrap_untrusted(value):
    content = json.dumps(redact(value), ensure_ascii=False, allow_nan=False)
    return '<untrusted_content>' + html.escape(content, quote=True) + '</untrusted_content>'
