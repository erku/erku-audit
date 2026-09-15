"""Deterministic rotation across the fixed audit skills.

`Worker.daily_audit()` used to run `self-redteam` every day. This module picks
a *different* meaningful, deterministic audit each day by rotating through
`ROTATION`, skipping any skill whose required inputs are not currently
available, and always falling back to `self-redteam` (which needs no inputs)
so a job is guaranteed.

Pure and side-effect free: no db access, no network writes. `build_inputs`
only ever *reads* (via `client.get`, already performed by the caller through
`listings_details`, and via a local read of `config/policy.json`) and never
mutates anything. Untrusted community text (listing titles/conditions) is
only ever carried as inert JSON data into the deterministic skills — it is
never interpreted or executed.

Dedup note for the caller: the caller already skips publishing an artifact
whose `hash` matches the last published hash *for that audit type*. This
module keeps `label` (and `skill`) stable for a given audit type so that
dedup keys off it correctly. A listing-specific audit -- one whose job
carries a `listing_id` -- is about a specific piece of live evidence and is
therefore always worth publishing even if its artifact `hash` happens to
match a prior generic run of the same skill; that publish decision belongs
to the caller, this module only surfaces `listing_id` so the caller can act
on it.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROTATION = ("self-redteam", "rail-audit", "leak-probe", "gate-probe")  # fixed, deterministic order

_POLICY_PATH = Path(__file__).resolve().parent.parent / "config" / "policy.json"
_MAX_LEAK_ROWS = 50
_MAX_FIELD_LEN = 500


def input_fingerprint(job: dict) -> str:
    """Return the stable fingerprint of the audit evidence inputs.

    Cursor position and publication state do not affect evidence, so they are
    deliberately excluded.  This value is safe to retain in local audit state
    and lets operators distinguish a repeated check from a check with changed
    inputs before publishing it.
    """
    payload = {
        "audit_type": job["skill"],
        "target": job["target"],
        "params": job["params"],
        "listing_id": job.get("listing_id"),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _rail_ready(rail):
    try:
        if not isinstance(rail, dict):
            return False
        awards, receipts = rail.get("awards"), rail.get("receipts")
        return isinstance(awards, list) and bool(awards) and isinstance(receipts, list) and bool(receipts)
    except (TypeError, ValueError, AttributeError):
        return False


def _leak_ready(target):
    try:
        return isinstance(target, dict) and bool(target)
    except (TypeError, ValueError, AttributeError):
        return False


def _gate_ready(tokens):
    try:
        return isinstance(tokens, list) and bool(tokens) and all(isinstance(t, str) and t for t in tokens)
    except (TypeError, ValueError, AttributeError):
        return False


def _rail_listing_id(rail):
    try:
        lid = rail.get("listing_id")
        if lid is None or isinstance(lid, bool):
            return None
        lid_int = int(lid)
        return lid_int if lid_int > 0 else None
    except (TypeError, ValueError, AttributeError):
        return None


def plan_next_audit(cursor: int, inputs: dict) -> dict:
    """Pure. Given a persisted integer cursor and available `inputs`, pick the
    next runnable audit by advancing through ROTATION starting at cursor%len.
    `inputs` keys (all optional): 'rail' (a job target dict with awards+receipts
    or None), 'leak_target' (dict or None), 'gate_target' (dict or None).
    Returns {'skill','target','params','label','cursor_next', 'listing_id'(optional)}
    for the first runnable skill found scanning at most len(ROTATION) steps from
    cursor; self-redteam is always runnable and is the guaranteed fallback so a
    job is ALWAYS returned. `label` is a seal-safe slug matching
    r'[a-z0-9][a-z0-9-]{0,63}' e.g. 'rail-audit-27' or 'self-redteam'.
    `cursor_next` is the cursor to persist for the following day (advance past
    the chosen skill). Never raises on malformed inputs -- treat as unavailable.
    """
    try:
        cursor = int(cursor)
    except (TypeError, ValueError):
        cursor = 0
    if not isinstance(inputs, dict):
        inputs = {}
    n = len(ROTATION)

    for step in range(n):
        idx = (cursor + step) % n
        skill = ROTATION[idx]
        cursor_next = (idx + 1) % n

        if skill == "self-redteam":
            return {"skill": skill, "target": {}, "params": {}, "label": "self-redteam", "cursor_next": cursor_next}

        if skill == "rail-audit":
            rail = inputs.get("rail")
            if not _rail_ready(rail):
                continue
            listing_id = _rail_listing_id(rail)
            target = {"awards": rail["awards"], "receipts": rail["receipts"]}
            label = f"rail-audit-{listing_id}" if listing_id is not None else "rail-audit"
            job = {"skill": skill, "target": target, "params": {}, "label": label, "cursor_next": cursor_next}
            if listing_id is not None:
                job["listing_id"] = listing_id
            return job

        if skill == "leak-probe":
            leak_target = inputs.get("leak_target")
            if not _leak_ready(leak_target):
                continue
            return {"skill": skill, "target": leak_target, "params": {}, "label": "leak-probe", "cursor_next": cursor_next}

        if skill == "gate-probe":
            gate_tokens = inputs.get("gate_target")
            if not _gate_ready(gate_tokens):
                continue
            target = {"blocked_tokens": list(gate_tokens)}
            return {"skill": skill, "target": target, "params": {}, "label": "gate-probe", "cursor_next": cursor_next}

    # Unreachable in practice: self-redteam is always runnable and always
    # inside the len(ROTATION) steps scanned above. Kept as a defensive
    # fallback so this function can never fail to return a job.
    return {"skill": "self-redteam", "target": {}, "params": {}, "label": "self-redteam", "cursor_next": (cursor + 1) % n}


def _blocked_tokens_from_policy():
    try:
        raw = json.loads(_POLICY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    tokens = raw.get("blocked_tokens")
    if _gate_ready(tokens):
        return list(tokens)
    return None


def _listing_id_of(row):
    return row.get("listing_id", row.get("id"))


def build_inputs(client, listings_details: list) -> dict:
    """Pure-ish helper (only READS via client.get, never writes). From already
    fetched open-listing detail dicts, derive:
      - 'rail': the first listing detail exposing non-empty awards+receipts as a
        {'awards':..., 'receipts':...} target, plus its listing_id; else None.
      - 'leak_target': a bounded dict of public fields worth scanning (titles +
        conditions of the listings), else None.
      - 'gate_target': blocked_tokens from config policy if present else None.
    Must tolerate missing keys and non-dict rows. Never let untrusted listing
    text become code -- you only pass it as data into the skills.

    `client` is accepted for interface symmetry with the caller's other
    build-from-live-data helpers and reserved for future direct reads; today
    everything needed is already present in `listings_details` plus the local
    policy config, so no additional `client.get` call is made here.
    """
    del client  # not currently needed; see docstring.

    rail = None
    leak_rows = []
    rows = listings_details if isinstance(listings_details, list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue

        if rail is None:
            awards, receipts = row.get("awards"), row.get("receipts")
            if isinstance(awards, list) and awards and isinstance(receipts, list) and receipts:
                candidate = {"awards": awards, "receipts": receipts}
                listing_id = _listing_id_of(row)
                if listing_id is not None:
                    candidate["listing_id"] = listing_id
                rail = candidate

        entry = {}
        title, condition = row.get("title"), row.get("condition")
        if isinstance(title, str) and title:
            entry["title"] = title[:_MAX_FIELD_LEN]
        if isinstance(condition, str) and condition:
            entry["condition"] = condition[:_MAX_FIELD_LEN]
        if entry:
            listing_id = _listing_id_of(row)
            if listing_id is not None:
                entry["listing_id"] = listing_id
            leak_rows.append(entry)

    leak_target = {"listings": leak_rows[:_MAX_LEAK_ROWS]} if leak_rows else None

    return {
        "rail": rail,
        "leak_target": leak_target,
        "gate_target": _blocked_tokens_from_policy(),
    }
