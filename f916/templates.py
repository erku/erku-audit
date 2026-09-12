"""Operator-curated bounty-template allowlist.

The allowlist in ``config/bounty_templates.json`` -- not the listing author --
is the trust boundary. Matching and target-building use ONLY structured
signals already present on an already-fetched listing; no prose is parsed
into parameters.

This module is pure except for the explicit, defensively-guarded
load-from-disk in ``load_templates``.
"""
from __future__ import annotations

import json
from pathlib import Path

_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "bounty_templates.json"

VERDICTS = ("BID", "CAUTION", "SKIP")


def load_templates(path=None) -> list:
    """Load the allowlist from config/bounty_templates.json (path defaults to
    that file resolved relative to the repo). Returns [] on any read/parse
    error; never raises."""
    target = Path(path) if path is not None else _DEFAULT_PATH
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return raw if isinstance(raw, list) else []


def match_template(listing: dict, templates: list) -> dict | None:
    """Return the first template whose STRUCTURED signals match `listing`,
    else None. Match = (template has no 'funder' OR listing['funder'] ==
    template['funder']) AND every substring in template['title_contains']
    appears in str(listing.get('title', '')).casefold(). Deterministic;
    tolerates missing keys / a non-dict listing or templates list (returns
    None)."""
    if not isinstance(listing, dict) or not isinstance(templates, list):
        return None
    title = str(listing.get("title", "")).casefold()
    for template in templates:
        if not isinstance(template, dict):
            continue
        funder = template.get("funder")
        if funder is not None and listing.get("funder") != funder:
            continue
        # A curated template must name at least one required title substring
        # -- an empty/missing list would vacuously match every listing,
        # which is unsafe for an allowlist that is the trust boundary.
        substrings = template.get("title_contains")
        if not isinstance(substrings, list) or not substrings:
            continue
        if all(isinstance(s, str) and s in title for s in substrings):
            return template
    return None


def _identity_int(value):
    """Extract a positive int identity from an int, a numeric string, or the
    API's "listing-<n>" resource-id form. Returns None on anything else.
    (Mirrors f916.opportunities._identity_int; duplicated here so this
    module stays free of a circular import.)"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        candidate = value[len("listing-"):] if value.startswith("listing-") else value
        if candidate.isdigit():
            n = int(candidate)
            return n if n > 0 else None
    return None


def rail_self_report(listing: dict) -> dict:
    """Build the rail-report skill TARGET from the listing's OWN
    already-fetched public fields (no network, no prose parsing).

    Never raises for a well-formed listing missing economic fields; raises
    ValueError('no_listing_id') only when the listing carries no usable id
    (the caller only calls this after a template matched a real listing)."""
    if not isinstance(listing, dict):
        raise ValueError("no_listing_id")
    listing_id = _identity_int(listing.get("listing_id"))
    if listing_id is None:
        listing_id = _identity_int(listing.get("id"))
    if listing_id is None:
        raise ValueError("no_listing_id")

    economics = listing.get("economics")
    economics = economics if isinstance(economics, dict) else {}

    quoted = {}
    if "funding_mode" in listing:
        quoted["funding_mode"] = listing["funding_mode"]
    elif "funder_address" in listing or "funds_seen_atomic" in listing:
        if "funder_address" in listing:
            quoted["funder_address"] = listing["funder_address"]
        if "funds_seen_atomic" in listing:
            quoted["funds_seen_atomic"] = listing["funds_seen_atomic"]

    if "settlement_version" in listing:
        quoted["settlement_version"] = listing["settlement_version"]
    elif "settlement_mode" in listing:
        quoted["settlement_mode"] = listing["settlement_mode"]
    elif "outstanding_awarded_atomic" in economics:
        quoted["outstanding_awarded_atomic"] = economics["outstanding_awarded_atomic"]

    if "state" in listing:
        quoted["lifecycle"] = listing["state"]
    elif "economic_state" in listing:
        quoted["lifecycle"] = listing["economic_state"]

    if "available_award_capacity" in economics:
        quoted["available_award_capacity"] = economics["available_award_capacity"]

    # Verdict inputs come from the same already-fetched, stranger-checkable
    # listing payload as `quoted` (no new lookups, no prose parsing).
    capacity = economics.get("available_award_capacity")
    withdrawn = listing.get("withdrawn_at")
    expired = listing.get("expired")
    escrow_address = listing.get("escrow_address")
    funding_mode = listing.get("funding_mode")
    outstanding = economics.get("outstanding_awarded_atomic")
    has_capacity = isinstance(capacity, (int, float)) and not isinstance(capacity, bool) and capacity > 0

    if withdrawn or expired or capacity == 0:
        verdict = "SKIP"
        basis = f"Listing is withdrawn, expired, or exhausted (available_award_capacity={capacity!r})."
    elif escrow_address and has_capacity:
        verdict = "BID"
        basis = f"Escrow funding is present with available_award_capacity={capacity!r}."
    elif funding_mode == "promise" and outstanding in (None, "0") and has_capacity:
        verdict = "CAUTION"
        basis = (f"funding_mode='promise' with outstanding_awarded_atomic={outstanding!r} "
                 f"and available_award_capacity={capacity!r}; no escrow evidence.")
    else:
        verdict = "SKIP"
        basis = "No stranger-checkable escrow or promise evidence supports bidding."

    return {
        "listing_id": listing_id,
        "quoted": quoted,
        "verdict": verdict,
        "verdict_basis": basis,
        "source": f"GET /api/listings/{listing_id} (re-checkable by any stranger)",
    }


def rail_derivation_check(listing: dict) -> dict:
    """Build the rail-derivation-check skill TARGET from the listing's OWN
    already-fetched public economics fields (no network, no prose parsing).

    Never raises for a well-formed listing missing an economics block;
    raises ValueError('no_listing_id') only when the listing carries no
    usable id (the caller only calls this after a template matched a real
    listing)."""
    if not isinstance(listing, dict):
        raise ValueError("no_listing_id")
    listing_id = _identity_int(listing.get("listing_id"))
    if listing_id is None:
        listing_id = _identity_int(listing.get("id"))
    if listing_id is None:
        raise ValueError("no_listing_id")

    economics = listing.get("economics")
    economics = economics if isinstance(economics, dict) else {}

    return {"listing_id": listing_id, "economics": economics}


def batch_cadence(listing: dict) -> dict:
    """Build the batch-cadence skill TARGET from the listing's OWN id.

    The listing carries no receipts itself -- they are walked live from the
    public GET /api/payouts feed. `walk: 'payouts'` is a marker for
    OpportunityRunner.build_artifact to populate `receipts` before running
    the skill (see opportunities.walk_payout_receipts).

    Raises ValueError('no_listing_id') only when the listing carries no
    usable id (the caller only calls this after a template matched a real
    listing)."""
    if not isinstance(listing, dict):
        raise ValueError("no_listing_id")
    listing_id = _identity_int(listing.get("listing_id"))
    if listing_id is None:
        listing_id = _identity_int(listing.get("id"))
    if listing_id is None:
        raise ValueError("no_listing_id")

    return {
        "listing_id": listing_id,
        "window_seconds": 60,
        "source": "GET https://1f916.ai/api/payouts",
        "walk": "payouts",
    }


BUILDERS = {"rail_self_report": rail_self_report, "rail_derivation_check": rail_derivation_check,
            "batch_cadence": batch_cadence}
