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
`selfext_seen`, so a gap already acted on (successfully or not) is never
reprocessed.
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from . import codescan
from .sandbox_client import SandboxClient

# The operator-curated allowlist -- this engine reads it only to know it
# exists conceptually, but NEVER writes to it. Auto-added tier-1 templates
# go to a separate agent-runtime store instead (see _AUTO_TEMPLATES_PATH),
# which f916.templates.load_templates merges in behind the operator file.
# Kept as a module-level attribute (rather than a hardcoded literal) so
# tests can monkeypatch it to a temp file.
_AUTO_TEMPLATES_PATH = Path(__file__).resolve().parent.parent / "config" / "auto_templates.json"

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


def _today_proposal_count(db):
    """Count today's tier-2 self_extend events (any status) so the daily
    proposal cap bounds LLM cost across process restarts, not just within a
    single call."""
    start_of_day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    count = 0
    for event in db.events("self_extend", limit=1000):
        data = event.get("data") if isinstance(event, dict) else None
        if isinstance(data, dict) and data.get("tier") == "skill" and event.get("created_at", 0) >= start_of_day:
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
    (dedup via the db setting `selfext_seen`):

      - `suggestion == 'template'` -- TIER 1: if under
        `settings.self_extend_max_templates` and the class maps to an
        EXISTING allowlisted pure skill (chosen by the gap's structured
        tokens), append a template to `config/bounty_templates.json`
        (auto, config-only). Skip if no safe existing skill fits, or the
        cap is already reached.
      - `suggestion == 'skill'` -- TIER 2: respect
        `settings.self_extend_max_proposals_per_day` (counts today's
        self_extend proposal events; stops attempting further generation
        once at cap). Calls `brain.generate_skill(gap)`; an empty result is
        logged and skipped. Otherwise the source is AST-scanned
        (`codescan.is_pure_skill_source`) -- a rejection is logged with
        reasons and the sandbox is NEVER invoked. A source that passes the
        scan has its tests run in the isolated sandbox
        (`sandbox_client_cls or SandboxClient`); a proposal record and
        on-disk copy are written for human review either way. Status is
        'ready_to_merge' iff automerge is on AND the scan passed AND the
        sandbox tests passed; otherwise 'proposed'. The generated source is
        NEVER imported or exec'd in this process, and nothing here
        git-merges or redeploys.

    Never raises out of a single gap's processing -- failures are logged
    under kind 'self_extend' with status 'error'. Returns a summary
    `{'templates_added': int, 'proposals': int}`.
    """
    summary = {"templates_added": 0, "proposals": 0}
    if not isinstance(gaps, list):
        return summary

    sandbox_cls = sandbox_client_cls or SandboxClient
    seen = _seen_classes(db)
    proposals_today = _today_proposal_count(db)
    max_proposals = getattr(settings, "self_extend_max_proposals_per_day", 0)

    for gap in gaps[:MAX_GAPS_PER_CALL]:
        try:
            if not isinstance(gap, dict):
                continue
            class_key = _gap_str(gap, "class_key")
            if not class_key or class_key in seen:
                continue

            suggestion = gap.get("suggestion")
            if suggestion == "template":
                if _tier1(gap, class_key, settings, db):
                    summary["templates_added"] += 1
                _mark_seen(db, seen, class_key)
            elif suggestion == "skill":
                if proposals_today >= max_proposals:
                    db.log("self_extend", {"tier": "skill", "class_key": class_key, "status": "capped"})
                    proposals_today += 1
                    _mark_seen(db, seen, class_key)
                    continue
                proposals_today += 1
                if _tier2(gap, class_key, settings, db, brain, sandbox_cls):
                    summary["proposals"] += 1
                _mark_seen(db, seen, class_key)
            else:
                _mark_seen(db, seen, class_key)
        except Exception as exc:
            db.log("self_extend", {"status": "error", "error_type": type(exc).__name__,
                                    "class_key": gap.get("class_key") if isinstance(gap, dict) else None})

    return summary
