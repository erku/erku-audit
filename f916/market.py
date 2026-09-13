"""Market radar: observe public earnings and detect capability gaps.

Everything this module reads comes from public GET endpoints (/api/payouts,
/api/listings and listing details). Every field pulled from those responses
is treated as DATA ONLY -- titles, funders and other free text are never
parsed into commands or executed; they are only tokenized for a
deterministic fingerprint (`class_key`) or substring-matched against a fixed
allowlist of hint words. No prose from a listing is ever interpreted as an
instruction.

`scan_market` is defensive by construction: every network call and every
row is guarded, malformed input is skipped rather than raised, and all walks
are bounded. It never raises.
"""
from __future__ import annotations

import re
import time

from .opportunities import _identity_int, evaluate_opportunity, walk_payout_receipts  # noqa: F401  (walk_payout_receipts kept for reuse/back-compat)
from .verifier import eligible

_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "for", "of", "to", "in", "on", "with",
    "is", "are", "this", "that", "your", "our", "from", "by", "at", "as",
    "be", "it", "its",
})
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_TEMPLATE_HINTS = ("rail", "economic", "payouts", "quote", "census", "stranger", "cadence",
                   "receipt", "anatomy", "binding")


def class_key(listing: dict) -> str:
    """Deterministic class fingerprint from STRUCTURED signals only.

    f"{funder}|{'-'.join(top title tokens)}". `funder` is lowercased; the
    title tokens are the first ~5 alphanumeric tokens of the title
    (casefolded, stopwords dropped), sorted, then joined with '-'. Only the
    `funder` and `title` fields are read -- free-body prose (`condition`,
    `audit`, etc.) never affects the key. Never raises.
    """
    try:
        if not isinstance(listing, dict):
            return "|"
        funder = str(listing.get("funder") or "").strip().casefold()
        title = str(listing.get("title") or "")
        tokens = [tok for tok in _TOKEN_RE.findall(title.casefold()) if tok not in _STOPWORDS]
        top = sorted(tokens[:5])
        return f"{funder}|{'-'.join(top)}"
    except Exception:
        return "|"


def _walk_payouts(client, max_pages=50, max_rows=5000):
    """Paginate GET /api/payouts, collecting raw rows (handle + amount_atomic
    included) so earners can be attributed. Mirrors the bounding and
    pagination contract of opportunities.walk_payout_receipts, but keeps the
    extra fields that helper drops. Tolerates any client error; never
    raises."""
    rows = []
    since_id = None
    try:
        for _ in range(max_pages):
            params = {"since_id": since_id} if since_id is not None else None
            response = client.get("/api/payouts", params=params)
            if not isinstance(response, dict):
                break
            bindings = response.get("bindings")
            if isinstance(bindings, list):
                for row in bindings:
                    if isinstance(row, dict):
                        rows.append(row)
                        if len(rows) >= max_rows:
                            return rows
            if not response.get("has_more"):
                break
            since_id = response.get("next_since_id")
    except Exception:
        pass
    return rows


def _earners_from_payouts(rows):
    """Group settled (receipt_id present) payouts rows by handle ->
    {'paid_count', 'total_atomic'}. Malformed rows are skipped."""
    earners = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("receipt_id") is None:
            continue
        handle = row.get("handle")
        if not isinstance(handle, str) or not handle:
            continue
        try:
            amount = int(row.get("amount_atomic"))
        except (TypeError, ValueError):
            amount = 0
        entry = earners.setdefault(handle, {"paid_count": 0, "total_atomic": 0})
        entry["paid_count"] += 1
        entry["total_atomic"] += amount
    return earners


def _open_listing_ids(client, max_listings):
    try:
        response = client.get("/api/listings")
    except Exception:
        return []
    summaries = response.get("listings") if isinstance(response, dict) else None
    summaries = summaries if isinstance(summaries, list) else []
    now_seconds = int(time.time())
    identities = []
    for summary in summaries:
        if len(identities) >= max_listings:
            break
        if not isinstance(summary, dict) or summary.get("withdrawn_at"):
            continue
        try:
            expiry = int(summary.get("expiry") or 0)
        except (TypeError, ValueError):
            expiry = 0
        if expiry <= now_seconds:
            continue
        listing_num = _identity_int(summary.get("listing_id", summary.get("id")))
        if listing_num is None:
            continue
        identities.append(listing_num)
    return identities


def _accumulate_class_stats(details):
    """Accumulate per-class_key paid-award stats from an iterable of
    already-fetched listing DETAIL dicts (no network). Returns
    (class_stats, sample_by_key) where sample_by_key holds one full listing
    detail per class_key for later gap evaluation. Malformed entries are
    skipped; never raises for a well-formed iterable."""
    class_stats = {}
    sample_by_key = {}
    for detail in details:
        if not isinstance(detail, dict):
            continue
        key = class_key(detail)
        entry = class_stats.setdefault(key, {
            "paid_handles": set(),
            "paid_total_atomic": 0,
            "sample_listing_id": detail.get("listing_id", detail.get("id")),
            "sample_title": detail.get("title"),
            "funder": detail.get("funder"),
        })
        sample_by_key.setdefault(key, detail)
        submissions = detail.get("submissions")
        submissions = submissions if isinstance(submissions, list) else []
        handle_by_submission = {}
        for row in submissions:
            if isinstance(row, dict) and isinstance(row.get("handle"), str) and row.get("handle"):
                handle_by_submission[row.get("id")] = row["handle"]
        awards = detail.get("awards")
        awards = awards if isinstance(awards, list) else []
        for award in awards:
            if not isinstance(award, dict) or award.get("state") != "paid":
                continue
            handle = handle_by_submission.get(award.get("submission_id"))
            if not isinstance(handle, str) or not handle:
                continue
            try:
                amount = int(award.get("amount_atomic"))
            except (TypeError, ValueError):
                amount = 0
            entry["paid_handles"].add(handle)
            entry["paid_total_atomic"] += amount
    for entry in class_stats.values():
        entry["paid_handles"] = sorted(entry["paid_handles"])
    return class_stats, sample_by_key


def _scan_listing_classes(client, max_listings):
    """Fetch up to `max_listings` open-listing details and accumulate
    per-class_key paid-award stats via `_accumulate_class_stats`. Returns
    (class_stats, sample_by_key)."""
    details = []
    for listing_num in _open_listing_ids(client, max_listings):
        try:
            detail = client.get(f"/api/listings/{listing_num}")
        except Exception:
            continue
        if isinstance(detail, dict):
            details.append(detail)
    return _accumulate_class_stats(details)


def _suggest(funder, title):
    """'template' if the class looks servable by an existing skill (a
    rail/economic/payouts/quote/census/stranger/cadence pattern hits the
    title or funder tokens), else 'skill'. Substring match only -- no prose
    is parsed into parameters."""
    text = f"{funder or ''} {title or ''}".casefold()
    return "template" if any(hint in text for hint in _TEMPLATE_HINTS) else "skill"


def _detect_gaps(class_stats, sample_by_key, own_handle):
    gaps = []
    for key, entry in class_stats.items():
        other_paid = [h for h in entry["paid_handles"] if h.casefold() != own_handle]
        if not other_paid:
            continue
        sample = sample_by_key.get(key)
        if not isinstance(sample, dict):
            continue
        try:
            evaluation = evaluate_opportunity(sample)
        except Exception:
            continue
        if evaluation.get("classification") != "unsupported":
            continue
        try:
            servable_by_verifier = bool(eligible(sample))
        except Exception:
            servable_by_verifier = False
        if servable_by_verifier:
            continue
        gaps.append({
            "class_key": key,
            "funder": entry.get("funder"),
            "sample_listing_id": entry.get("sample_listing_id"),
            "sample_title": entry.get("sample_title"),
            "paid_handles": other_paid,
            "paid_total_atomic": entry.get("paid_total_atomic", 0),
            "our_classification": evaluation.get("classification"),
            "suggestion": _suggest(entry.get("funder"), entry.get("sample_title")),
        })
    return gaps


def scan_market(client, db, settings, *, listing_details=None, max_listings=25) -> dict:
    """Build market intelligence from public data only. Bounded, defensive,
    never raises. See module docstring. Returns
    {'earners': {...}, 'class_stats': {...}, 'gaps': [...]}. Logs nothing
    itself -- the caller logs the result.

    `listing_details`, when provided (a list of already-fetched listing
    DETAIL dicts, e.g. Worker.cycle's per-cycle open-listing fetch), is
    REUSED for the class_stats/gap scan instead of re-fetching
    /api/listings + each listing's detail -- this is what lets the radar
    run every cycle cheaply. Only the `/api/payouts` walk (bounded) still
    runs fresh every call, since it is not part of the per-cycle listing
    fetch. When `listing_details` is None (the default), the original
    self-fetching path runs unchanged, for the daily-maintenance/standalone
    caller."""
    earners, class_stats, gaps = {}, {}, []
    try:
        own_handle = str(getattr(settings, "handle", "") or "").casefold()
        try:
            earners = _earners_from_payouts(_walk_payouts(client))
        except Exception:
            earners = {}
        try:
            if listing_details is not None:
                class_stats, sample_by_key = _accumulate_class_stats(listing_details)
            else:
                class_stats, sample_by_key = _scan_listing_classes(client, max_listings)
        except Exception:
            class_stats, sample_by_key = {}, {}
        try:
            gaps = _detect_gaps(class_stats, sample_by_key, own_handle)
        except Exception:
            gaps = []
    except Exception:
        pass
    return {"earners": earners, "class_stats": class_stats, "gaps": gaps}
