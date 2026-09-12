"""Outcome feedback loop: match our bounty submissions to a listing's awards,
record outcomes idempotently, and roll paid earnings into reward_metrics."""
from __future__ import annotations

_STATE_RANK = {"paid": 2, "awarded": 1, "payable": 1, "overdue_unpaid": 1, "submitted": 0}


def submitted_listing_ids(db) -> list[int]:
    """Listing ids we have submitted to, deduped, most-recent first."""
    seen = set(); ids = []
    try:
        events = db.events("action", 1000)
    except Exception:
        return ids
    for event in events:
        try:
            data = event.get("data") if isinstance(event, dict) else None
            intent = data.get("intent") if isinstance(data, dict) else None
            if not isinstance(intent, dict) or intent.get("action") != "submit": continue
            listing_id = intent.get("listing_id")
            if isinstance(listing_id, bool): continue
            if isinstance(listing_id, int): value = listing_id
            elif isinstance(listing_id, str) and listing_id.strip().lstrip("-").isdigit(): value = int(listing_id)
            else: continue
            if value in seen: continue
            seen.add(value); ids.append(value)
        except Exception:
            continue
    return ids


def classify_outcome(detail: dict, handle: str) -> dict | None:
    """Find our submissions on this listing detail and return the strongest outcome."""
    try:
        if not isinstance(detail, dict) or not handle: return None
        submissions = detail.get("submissions")
        awards = detail.get("awards")
        submissions = submissions if isinstance(submissions, list) else []
        awards = awards if isinstance(awards, list) else []
        our_ids = set()
        for row in submissions:
            if not isinstance(row, dict): continue
            row_handle = row.get("handle")
            if isinstance(row_handle, str) and row_handle.casefold() == str(handle).casefold():
                sid = row.get("id")
                if isinstance(sid, int): our_ids.add(sid)
        if not our_ids: return None
        listing_id = detail.get("listing_id", detail.get("id"))
        best = None; best_rank = -1
        for award in awards:
            if not isinstance(award, dict): continue
            sid = award.get("submission_id")
            if sid not in our_ids: continue
            state = award.get("state")
            rank = _STATE_RANK.get(state, -1)
            if rank > best_rank:
                best_rank = rank
                amount = award.get("amount_atomic")
                best = {
                    "listing_id": listing_id,
                    "submission_id": sid,
                    "state": state,
                    "amount_atomic": str(amount) if amount is not None else None,
                    "token": award.get("token"),
                    "paid": state == "paid",
                }
        if best is not None: return best
        return {
            "listing_id": listing_id,
            "submission_id": sorted(our_ids)[0],
            "state": "submitted",
            "amount_atomic": None,
            "token": None,
            "paid": False,
        }
    except Exception:
        return None


def usd_from_award(state, amount_atomic, token, chain_id) -> float:
    """Earned USD for a paid USDC-on-Base award; 0.0 for anything else or bad input."""
    try:
        if state != "paid": return 0.0
        if not isinstance(token, str) or token.strip().casefold() != "usdc": return 0.0
        if chain_id is not None and int(chain_id) != 8453: return 0.0
        amount = int(amount_atomic)
        if amount < 0: return 0.0
        return amount / 1_000_000
    except Exception:
        return 0.0


def _logged_outcome_triples(db):
    triples = set()
    try:
        events = db.events("outcome", 1000)
    except Exception:
        return triples
    for event in events:
        try:
            data = event.get("data") if isinstance(event, dict) else None
            triples.add((int(data["listing_id"]), int(data["submission_id"]), str(data["state"])))
        except Exception:
            continue
    return triples


def scan_outcomes(client, db, settings) -> dict:
    summary = {"scanned": 0, "new_outcomes": 0, "earnings_usd": 0.0}
    handle = getattr(settings, "handle", None)
    listing_ids = submitted_listing_ids(db)[:25]
    seen_triples = _logged_outcome_triples(db)
    for listing_id in listing_ids:
        summary["scanned"] += 1
        try:
            detail = client.get(f"/api/listings/{int(listing_id)}")
        except Exception:
            continue
        outcome = classify_outcome(detail, handle)
        if outcome is None: continue
        try:
            triple = (int(outcome["listing_id"]), int(outcome["submission_id"]), str(outcome["state"]))
        except Exception:
            continue
        if triple in seen_triples: continue
        to_log = dict(outcome)
        to_log["chain_id"] = detail.get("chain_id") if isinstance(detail, dict) else None
        try:
            db.log("outcome", to_log)
        except Exception:
            continue
        seen_triples.add(triple)
        summary["new_outcomes"] += 1
    # Recompute earnings from all logged outcome events (not incremented) so
    # re-running this function is idempotent even after partial failures.
    earnings = 0.0; paid_pairs = set()
    try:
        events = db.events("outcome", 1000)
    except Exception:
        events = []
    for event in events:
        try:
            data = event.get("data") if isinstance(event, dict) else None
            if not isinstance(data, dict) or data.get("state") != "paid": continue
            pair = (int(data["listing_id"]), int(data["submission_id"]))
        except Exception:
            continue
        if pair in paid_pairs: continue
        paid_pairs.add(pair)
        earnings += usd_from_award(data.get("state"), data.get("amount_atomic"), data.get("token"), data.get("chain_id"))
    summary["earnings_usd"] = earnings
    try:
        metrics = db.get_setting("reward_metrics", {})
        metrics = dict(metrics) if isinstance(metrics, dict) else {}
        metrics["outside_funded_earnings_usd"] = earnings
        db.set_setting("reward_metrics", metrics)
    except Exception:
        pass
    return summary
