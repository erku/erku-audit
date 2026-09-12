"""Pure, deterministic helpers for Ollama rate-limit recovery. No I/O."""
from __future__ import annotations

from email.utils import parsedate_to_datetime


def parse_retry_after(value, now):
    """Return epoch-seconds float when the model may be retried, or None.
    Accepts an int/str number of seconds ('30' -> now+30) and an HTTP-date
    string (RFC 7231, via email.utils.parsedate_to_datetime -> epoch). None on
    anything unparseable. Never raises."""
    if value is None:
        return None
    try:
        seconds = float(str(value).strip())
        return now + seconds
    except (TypeError, ValueError):
        pass
    try:
        parsed = parsedate_to_datetime(str(value).strip())
        if parsed is None:
            return None
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def backoff_seconds(attempts, base, cap):
    """Exponential backoff base*2**(attempts-1) clamped to [base, cap]. attempts>=1."""
    attempts = max(1, int(attempts))
    value = base * (2 ** (attempts - 1))
    return max(base, min(cap, value))


def record_rate_limit(state, retry_after_epoch, now, base, cap):
    """state is the persisted dict (or {}). Returns a NEW dict with:
      attempts = prev+1
      blocked_until = retry_after_epoch if given (and > now) else now+backoff_seconds(attempts,base,cap)
      last = now
    Bounded: blocked_until - now never exceeds cap when retry_after not supplied."""
    state = state or {}
    attempts = int(state.get("attempts") or 0) + 1
    if retry_after_epoch is not None and retry_after_epoch > now:
        blocked_until = retry_after_epoch
    else:
        blocked_until = now + backoff_seconds(attempts, base, cap)
    return {"attempts": attempts, "blocked_until": blocked_until, "last": now}


def is_blocked(state, now):
    """True iff state has a blocked_until in the future."""
    if not isinstance(state, dict):
        return False
    blocked_until = state.get("blocked_until")
    if blocked_until is None:
        return False
    try:
        return float(blocked_until) > now
    except (TypeError, ValueError):
        return False
