"""f916.worthwhile -- a cheap, deterministic ROI gate for the self-extension
engine (see f916/selfext.py).

Tier-2 skill generation calls an expensive LLM (`brain.generate_skill`).
Before paying that cost, `assess` decides whether a gap's likely reward can
recoup it, UNLESS the gap carries PRESTIGE (a reputation / future-value
signal) that is worth pursuing on its own even at low measured reward.

Pure and defensive by construction: every function here reads only the
gap's already-structured fields (`class_key`, `funder`, `sample_title`,
`paid_total_atomic`) and plain settings values, does no I/O, and never
raises.
"""
from __future__ import annotations


def _usd(atomic):
    """USDC atomic (str/int) -> float USD (/1e6); 0.0 on bad input."""
    try:
        return int(atomic) / 1_000_000
    except (TypeError, ValueError):
        return 0.0


def _gap_str(gap, key):
    value = gap.get(key) if isinstance(gap, dict) else None
    return value if isinstance(value, str) else ""


def has_prestige(gap: dict, settings) -> bool:
    """True iff the gap carries a reputation/future-value signal: its funder
    is in `settings.worthwhile_prestige_funders` (casefold), OR any
    substring in `settings.worthwhile_prestige_terms` (casefold) appears in
    the class_key, funder, or sample_title. Never raises."""
    try:
        funder = _gap_str(gap, "funder").casefold()
        prestige_funders = getattr(settings, "worthwhile_prestige_funders", ()) or ()
        if funder and funder in {str(f).casefold() for f in prestige_funders}:
            return True

        haystack = " ".join((
            _gap_str(gap, "class_key"),
            _gap_str(gap, "funder"),
            _gap_str(gap, "sample_title"),
        )).casefold()
        prestige_terms = getattr(settings, "worthwhile_prestige_terms", ()) or ()
        for term in prestige_terms:
            term = str(term).casefold().strip()
            if term and term in haystack:
                return True
    except Exception:
        return False
    return False


def assess(gap: dict, settings) -> dict:
    """Return {'worth':bool,'reward_usd':float,'prestige':bool,'reason':str}.

    reward_usd = _usd(gap.get('paid_total_atomic')). prestige =
    has_prestige(gap, settings). worth = prestige OR reward_usd >=
    settings.worthwhile_min_reward_usd. reason: 'prestige' | 'reward_ok' |
    'below_min_reward_no_prestige'. Never raises."""
    try:
        reward_usd = _usd(gap.get("paid_total_atomic") if isinstance(gap, dict) else None)
        prestige = has_prestige(gap, settings)
        min_reward = getattr(settings, "worthwhile_min_reward_usd", 0.0)
        if prestige:
            return {"worth": True, "reward_usd": reward_usd, "prestige": True, "reason": "prestige"}
        if reward_usd >= min_reward:
            return {"worth": True, "reward_usd": reward_usd, "prestige": False, "reason": "reward_ok"}
        return {"worth": False, "reward_usd": reward_usd, "prestige": False,
                "reason": "below_min_reward_no_prestige"}
    except Exception:
        return {"worth": False, "reward_usd": 0.0, "prestige": False, "reason": "below_min_reward_no_prestige"}
