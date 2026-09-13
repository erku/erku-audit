"""Self-extension engine: turns detected capability gaps into either

  * tier 1 -- an auto-added bounty TEMPLATE that routes a new listing class
    to an EXISTING allowlisted pure skill (config-only, bounded by a hard
    cap), or
  * tier 2 -- a sandboxed, AST-scanned PURE skill PROPOSAL for human review.

HARD SAFETY LINES (see docs/implementation-plan-v7.md Task X):
  - A generated skill is a PURE deterministic `(target, params) -> dict`
    function. It is NEVER imported or exec'd in this process. It is only
    ever run inside the isolated `SandboxClient`, against its own tests,
    and only after `codescan.is_pure_skill_source` has already approved it.
  - The generated source and tests are DATA. They are written to a db
    setting and to a proposal file under `<data_dir>/selfext-proposals/`
    for human review -- nothing here executes them directly.
  - Nothing here git-merges or redeploys live code. Even with
    `settings.self_extend_automerge` on, this module only marks a proposal
    record 'ready_to_merge' and logs it; the actual merge/deploy remains an
    external human/operator step.
  - Tier-1 templates may only route a gap's class to an ALREADY-allowlisted
    pure skill (`f916.skills.SKILLS`), bounded by
    `settings.self_extend_max_templates`.
  - The operator-curated `config/bounty_templates.json` is NEVER written by
    this engine. Auto-added tier-1 templates go to a separate store,
    `config/auto_templates.json`, which `f916.templates.load_templates`
    merges in (operator entries first, so a curated template always wins a
    match over an auto one).
  - `Brain.generate_skill` is seeded from the gap's STRUCTURED fields
    (class_key, funder, sample_title, suggestion) -- never raw listing body
    prose; see `f916/brain.py`.

Processing is idempotent per `class_key`, deduplicated via the db setting
`selfext_seen` -- but `selfext_seen` is only ever set on a genuine SUCCESS
(a tier-1 template added, or a tier-2 proposal record written). A gap that
fails for a FIXABLE reason (`generation_empty`, `scan_rejected`, `capped`,
`no_existing_skill`, `write_failed`, or an unexpected `error`) is instead
recorded in the db setting `selfext_attempts` with a bounded retry count and
a cooldown (`settings.self_extend_retry_max` /
`settings.self_extend_retry_cooldown_seconds`), so an engine improvement
(e.g. a widened scanner) can let a previously-failed gap be retried instead
of being silently lost forever.

Before the expensive tier-2 `brain.generate_skill` call, a cheap
deterministic ROI gate (`f916/worthwhile.py`) skips gaps whose measured
reward can't recoup the token cost, UNLESS the gap carries PRESTIGE
(reputation / future value) worth pursuing on its own. A low-ROI skip is
NOT a failure: it is not marked seen, not counted as an attempt, and does
not consume the daily proposal cap, so it is cheaply re-checked on every
scan and proceeds automatically once it becomes worthwhile.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from . import codescan
from . import worthwhile
from .sandbox_client import SandboxClient

# The operator-curated allowlist -- this engine reads it only to know it
# exists conceptually, but NEVER writes to it. Auto-added tier-1 templates
# go to a separate agent-runtime store instead (see _AUTO_TEMPLATES_PATH),
# which f916.templates.load_templates merges in behind the operator file.
# Kept as a module-level attribute (rather than a hardcoded literal) so
# tests can monkeypatch it to a temp file.
# Writable DATA_DIR store (repo /app/config is read-only in the container);
# must match f916.templates._AUTO_DEFAULT_PATH so load_templates reads what we write.
_AUTO_TEMPLATES_PATH = Path(os.getenv("DATA_DIR", "data")) / "auto_templates.json"

# Bound how much work a single call does, independent of the caller-supplied
# gap list length.
MAX_GAPS_PER_CALL = 25

# Route a gap to an EXISTING allowlisted pure skill by a structured keyword
# match against the gap's class_key / sample_title (never raw body prose).
# The matched keyword itself is used as the template's `title_contains`
# token, since it is guaranteed to be a safe, deterministic, structured
# value -- never text lifted from a listing's prose body.
_TIER1_ROUTES = (
    ("rail-derivation-check", "rail_derivation_check", ("rail", "economic")),
    ("rail-report", "rail_self_report", ("quote", "stranger", "census")),
    ("batch-cadence", "batch_cadence", ("cadence",)),
)


def _gap_str(gap, key):
    value = gap.get(key) if isinstance(gap, dict) else None
    return value if isinstance(value, str) else ""


def _class_tokens_text(gap):
    return (_gap_str(gap, "class_key") + " " + _gap_str(gap, "sample_title")).casefold()


def _pick_existing_skill(gap):
    """Return (skill, builder, token) for the first structured keyword that
    matches an ALREADY-allowlisted pure skill, else None."""
    from . import skills as skills_mod
    tokens_text = _class_tokens_text(gap)
    for skill, builder, keywords in _TIER1_ROUTES:
        if skill not in skills_mod.SKILLS:
            continue
        for keyword in keywords:
            if keyword in tokens_text:
                return skill, builder, keyword
    return None


def _gap_hash(gap):
    class_key = _gap_str(gap, "class_key")
    basis = class_key if class_key else json.dumps(gap, sort_keys=True, default=str)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def _load_templates(path):
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return raw if isinstance(raw, list) else []


def _save_templates(path, templates):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(templates, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _seen_classes(db):
    seen = db.get_setting("selfext_seen", [])
    return set(seen) if isinstance(seen, list) else set()


def _mark_seen(db, seen_set, class_key):
    seen_set.add(class_key)
    db.set_setting("selfext_seen", sorted(seen_set))


# Bound how many distinct failing class_keys we track retry state for, so a
# flood of one-off failures can't grow the setting unboundedly.
MAX_TRACKED_ATTEMPTS = 300


def _attempts(db):
    """Read the db setting 'selfext_attempts' -> {class_key: {'count':int,
    'last_ts':float}}. {} on missing/malformed data."""
    attempts = db.get_setting("selfext_attempts", {})
    return attempts if isinstance(attempts, dict) else {}


def _bump_attempt(db, attempts, class_key, now):
    """Record one more FIXABLE-failure attempt for `class_key` and persist.
    Bounds `attempts` to the most recently-failed MAX_TRACKED_ATTEMPTS
    class_keys (by last_ts) so the setting can't grow without bound."""
    prev = attempts.get(class_key)
    prev_count = prev.get("count", 0) if isinstance(prev, dict) else 0
    attempts[class_key] = {"count": prev_count + 1, "last_ts": now}
    if len(attempts) > MAX_TRACKED_ATTEMPTS:
        most_recent = sorted(
            attempts.items(),
            key=lambda item: item[1].get("last_ts", 0) if isinstance(item[1], dict) else 0,
            reverse=True,
        )[:MAX_TRACKED_ATTEMPTS]
        attempts.clear()
        attempts.update(most_recent)
    db.set_setting("selfext_attempts", attempts)


def _today_proposal_count(db):
    """Count today's tier-2 self_extend events that actually reached (or
    attempted) generation -- so the daily proposal cap bounds LLM cost
    across process restarts, not just within a single call. Excludes
    'skipped_low_roi' events: the ROI gate runs BEFORE generation is even
    attempted, so a low-ROI skip must never itself consume the cap it is
    guarding."""
    start_of_day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    count = 0
    for event in db.events("self_extend", limit=1000):
        data = event.get("data") if isinstance(event, dict) else None
        if (isinstance(data, dict) and data.get("tier") == "skill"
                and data.get("status") != "skipped_low_roi"
                and event.get("created_at", 0) >= start_of_day):
            count += 1
    return count


def _tier1(gap, class_key, settings, db):
    """Auto-add a bounty template routing `class_key` to an EXISTING
    allowlisted pure skill. Appended ONLY to the separate agent-runtime
    store `config/auto_templates.json` -- the operator-curated
    `config/bounty_templates.json` is never read or written here. Returns
    True iff a template was appended."""
    auto_templates = _load_templates(_AUTO_TEMPLATES_PATH)
    max_templates = getattr(settings, "self_extend_max_templates", 0)
    if len(auto_templates) >= max_templates:
        db.log("self_extend", {"tier": "template", "class_key": class_key, "status": "capped"})
        return False

    picked = _pick_existing_skill(gap)
    if picked is None:
        db.log("self_extend", {"tier": "template", "class_key": class_key, "status": "no_existing_skill"})
        return False
    skill, builder, token = picked

    new_template = {
        "id": f"selfext-{_gap_hash(gap)}",
        "skill": skill,
        "builder": builder,
        "title_contains": [token],
    }
    updated = auto_templates + [new_template]
    try:
        _save_templates(_AUTO_TEMPLATES_PATH, updated)
    except OSError:
        db.log("self_extend", {"tier": "template", "class_key": class_key, "skill": skill, "status": "write_failed"})
        return False

    db.log("self_extend", {"tier": "template", "class_key": class_key, "skill": skill, "status": "added"})
    return True


def _tier2(gap, class_key, settings, db, brain, sandbox_client_cls):
    """Generate, AST-scan, and sandbox-test a candidate pure skill for
    `class_key`. Writes a proposal record + files for human review; NEVER
    imports/execs the generated source, and NEVER merges or deploys it.
    Returns True iff a proposal record was written."""
    generated = brain.generate_skill(gap)
    func_name = generated.get("func_name") if isinstance(generated, dict) else None
    source = generated.get("source") if isinstance(generated, dict) else None
    tests = generated.get("tests") if isinstance(generated, dict) else None
    if not (isinstance(func_name, str) and isinstance(source, str) and isinstance(tests, str)
            and func_name and source and tests):
        db.log("self_extend", {"tier": "skill", "class_key": class_key, "proposed": "generation_empty"})
        return False

    scan_ok, reasons = codescan.is_pure_skill_source(source, func_name)
    if not scan_ok:
        db.log("self_extend", {"tier": "skill", "class_key": class_key, "scan_ok": False,
                                "status": "scan_rejected", "reasons": reasons})
        return False

    gap_hash = _gap_hash(gap)
    # The generated source is DATA handed to the isolated sandbox's own
    # filesystem IPC. It is never imported or exec'd in this process.
    sandbox = sandbox_client_cls(settings)
    result = sandbox.run(
        project_name=f"selfext-{gap_hash}",
        files={f"{func_name}.py": source, "tests/test_gen.py": tests},
        test_path="tests",
    )
    tests_passed = bool(isinstance(result, dict) and result.get("passed") is True)

    automerge = bool(getattr(settings, "self_extend_automerge", False))
    status = "ready_to_merge" if (automerge and scan_ok and tests_passed) else "proposed"

    record = {
        "class_key": class_key,
        "func_name": func_name,
        "source": source,
        "tests": tests,
        "scan_ok": scan_ok,
        "tests_passed": tests_passed,
        "status": status,
        "created_at": time.time(),
    }
    db.set_setting(f"selfext_proposal:{gap_hash}", record)

    try:
        proposal_dir = Path(settings.data_dir) / "selfext-proposals" / gap_hash
        proposal_dir.mkdir(parents=True, exist_ok=True)
        (proposal_dir / f"{func_name}.py").write_text(source, encoding="utf-8")
        (proposal_dir / "test_gen.py").write_text(tests, encoding="utf-8")
    except OSError:
        pass  # the db record is authoritative; the file copy is best-effort

    db.log("self_extend", {"tier": "skill", "class_key": class_key, "scan_ok": scan_ok,
                            "tests_passed": tests_passed, "status": status})
    return True


def act_on_gaps(gaps, settings, db, brain, sandbox_client_cls=None) -> dict:
    """Entry point called from maintenance when `settings.self_extend_enabled`.

    For each gap (bounded by MAX_GAPS_PER_CALL), idempotent per `class_key`
    via the db setting `selfext_seen` -- but `selfext_seen` is only ever set
    on a genuine SUCCESS (see module docstring). A gap already in
    `selfext_seen` is never reprocessed.

    A gap NOT in `selfext_seen` may still have a retry record in the db
    setting `selfext_attempts` (from a previous FIXABLE failure): if its
    attempt count has reached `settings.self_extend_retry_max`, it is
    permanently skipped (logged once per call as 'retry_exhausted'); if it
    is still within `settings.self_extend_retry_cooldown_seconds` of its
    last attempt, it is quietly skipped (no log spam) until the cooldown
    elapses, at which point it is retried.

      - `suggestion == 'template'` -- TIER 1: if under
        `settings.self_extend_max_templates` and the class maps to an
        EXISTING allowlisted pure skill (chosen by the gap's structured
        tokens), append a template to `config/bounty_templates.json`
        (auto, config-only). SUCCESS marks `selfext_seen`; a fixable miss
        (no existing skill fits, the cap is reached, or the write failed)
        instead bumps `selfext_attempts` so it can be retried later.
      - `suggestion == 'skill'` -- TIER 2: first, a cheap deterministic ROI
        gate (`f916/worthwhile.assess`) checks whether the gap's measured
        reward can recoup the cost of the expensive `brain.generate_skill`
        call, UNLESS the gap carries PRESTIGE (reputation / future value).
        A low-ROI gap is logged 'skipped_low_roi' and left completely
        untouched -- NOT marked seen, NOT counted as an attempt, and the
        daily proposal cap is NOT consumed -- so it is cheaply re-checked
        every scan and proceeds automatically once it becomes worthwhile.
        A worthwhile gap then respects
        `settings.self_extend_max_proposals_per_day` (counts today's
        self_extend proposal events; a gap hitting an already-reached cap
        is logged 'capped' and bumps `selfext_attempts` to retry another
        day). Otherwise calls `brain.generate_skill(gap)`; an empty result
        is logged 'generation_empty' and bumps the attempt. Otherwise the
        source is AST-scanned (`codescan.is_pure_skill_source`) -- a
        rejection is logged 'scan_rejected' with reasons (the sandbox is
        NEVER invoked) and bumps the attempt. A source that passes the scan
        has its tests run in the isolated sandbox (`sandbox_client_cls or
        SandboxClient`); a proposal record and on-disk copy are written for
        human review either way, and THIS counts as SUCCESS (marks seen)
        even when the sandbox tests failed -- the record is on file for
        human review. Status is 'ready_to_merge' iff automerge is on AND
        the scan passed AND the sandbox tests passed; otherwise 'proposed'.
        The generated source is NEVER imported or exec'd in this process,
        and nothing here git-merges or redeploys.
      - any other `suggestion` -- nothing actionable, marked seen.

    Never raises out of a single gap's processing -- an unexpected failure
    is a FIXABLE failure too: it is logged under kind 'self_extend' with
    status 'error' and bumps `selfext_attempts` (never marks seen). Returns
    a summary `{'templates_added': int, 'proposals': int}`.
    """
    summary = {"templates_added": 0, "proposals": 0}
    if not isinstance(gaps, list):
        return summary

    sandbox_cls = sandbox_client_cls or SandboxClient
    seen = _seen_classes(db)
    attempts = _attempts(db)
    now = time.time()
    retry_max = getattr(settings, "self_extend_retry_max", 0)
    cooldown = getattr(settings, "self_extend_retry_cooldown_seconds", 0)
    proposals_today = _today_proposal_count(db)
    max_proposals = getattr(settings, "self_extend_max_proposals_per_day", 0)

    for gap in gaps[:MAX_GAPS_PER_CALL]:
        class_key = None
        try:
            if not isinstance(gap, dict):
                continue
            class_key = _gap_str(gap, "class_key")
            if not class_key or class_key in seen:
                continue

            suggestion = gap.get("suggestion")
            attempt = attempts.get(class_key)
            if isinstance(attempt, dict):
                count = attempt.get("count", 0) if isinstance(attempt.get("count", 0), (int, float)) else 0
                last_ts = attempt.get("last_ts", 0) if isinstance(attempt.get("last_ts", 0), (int, float)) else 0
                if count >= retry_max:
                    # Log retry_exhausted ONCE EVER (persist a flag in the
                    # attempt record), not once per scan -- an exhausted gap
                    # keeps surfacing every scan and would otherwise spam the log.
                    if not attempt.get("exhausted_logged"):
                        log_data = {"class_key": class_key, "status": "retry_exhausted"}
                        if suggestion in ("template", "skill"):
                            log_data["tier"] = suggestion
                        db.log("self_extend", log_data)
                        attempt["exhausted_logged"] = True
                        db.set_setting("selfext_attempts", attempts)
                    continue
                if now - last_ts < cooldown:
                    continue

            if suggestion == "template":
                if _tier1(gap, class_key, settings, db):
                    summary["templates_added"] += 1
                    _mark_seen(db, seen, class_key)
                else:
                    _bump_attempt(db, attempts, class_key, now)
            elif suggestion == "skill":
                w = worthwhile.assess(gap, settings)
                if not w.get("worth"):
                    db.log("self_extend", {"tier": "skill", "class_key": class_key,
                                            "status": "skipped_low_roi",
                                            "reward_usd": w.get("reward_usd"),
                                            "prestige": w.get("prestige")})
                    continue
                if proposals_today >= max_proposals:
                    db.log("self_extend", {"tier": "skill", "class_key": class_key, "status": "capped"})
                    _bump_attempt(db, attempts, class_key, now)
                    continue
                proposals_today += 1
                if _tier2(gap, class_key, settings, db, brain, sandbox_cls):
                    summary["proposals"] += 1
                    _mark_seen(db, seen, class_key)
                else:
                    _bump_attempt(db, attempts, class_key, now)
            else:
                _mark_seen(db, seen, class_key)
        except Exception as exc:
            db.log("self_extend", {"status": "error", "error_type": type(exc).__name__,
                                    "class_key": class_key})
            if isinstance(class_key, str) and class_key:
                try:
                    _bump_attempt(db, attempts, class_key, now)
                except Exception:
                    pass

    return summary
