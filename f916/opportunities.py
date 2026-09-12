"""Conservative, restart-safe execution of listing-specific deterministic audits."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from invariants import redact
from .models import Intent
from .seal import seal_artifact
from .skills import SKILLS, run as run_skill
from .templates import BUILDERS, load_templates, match_template


AUDIT_SKILLS = frozenset({"gate-probe", "leak-probe", "rail-audit"})
PROJECT_TERMS = (
    "fix ", "patch ", "pull request", "open a pr", "repository", "implement ",
    "build ", "full suite", "regression test", "unpatched", "source code",
)


def _source(listing):
    """Select stable fields that define the requested work and its inputs."""
    return {
        key: listing[key]
        for key in ("listing_id", "id", "title", "condition", "payload_hash", "post_id", "audit")
        if key in listing
    }


def _source_hash(listing):
    encoded = json.dumps(_source(listing), sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _identity_int(value):
    """Extract a positive int identity from an int, a numeric string, or the
    API's ``"listing-<n>"`` resource-id form. Returns None on anything else."""
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


def evaluate_opportunity(listing):
    """Classify data only; community prose is never interpreted as a command."""
    if not isinstance(listing, dict):
        return {"classification": "unsupported", "reason": "listing_not_an_object", "source_hash": hashlib.sha256(b"null").hexdigest()}
    # The API serves listing_id as an int and id as the string "listing-<n>";
    # accept either form and reject only when two present identities disagree.
    primary = listing.get("listing_id")
    listing_id = _identity_int(primary if primary is not None else listing.get("id"))
    id_identity = _identity_int(listing.get("id")) if listing.get("id") is not None else None
    if listing_id is None or (id_identity is not None and primary is not None and id_identity != listing_id):
        return {"classification": "unsupported", "reason": "invalid_listing_identity", "source_hash": _source_hash(listing)}
    source_hash = _source_hash(listing)

    text = f'{listing.get("title", "")}\n{listing.get("condition", "")}'.casefold()
    if any(term in text for term in PROJECT_TERMS):
        return {"classification": "project_required", "reason": "requires_code_or_project_work",
                "listing_id": listing_id, "source_hash": source_hash}

    audit = listing.get("audit")
    if not isinstance(audit, dict):
        # No operator-provided structured audit. Fall back to the curated,
        # operator-maintained bounty-template allowlist -- the allowlist,
        # not the listing author, is the trust boundary here -- before
        # giving up as unsupported. Structured signals only; no prose is
        # parsed into parameters.
        templates = load_templates()
        tpl = match_template(listing, templates)
        if tpl is not None:
            try:
                target = BUILDERS[tpl["builder"]](listing)
            except (KeyError, ValueError):
                target = None
            if target is not None:
                return {"classification": "supported", "reason": "curated_template",
                        "listing_id": listing_id, "source_hash": source_hash,
                        "skill": tpl.get("skill", "rail-report"), "target": target,
                        "params": {}, "template_id": tpl["id"]}
        return {"classification": "unsupported", "reason": "no_structured_safe_audit",
                "listing_id": listing_id, "source_hash": source_hash}
    skill, target, params = audit.get("skill"), audit.get("target"), audit.get("params", {})
    if skill not in AUDIT_SKILLS or skill not in SKILLS or not isinstance(target, dict) or not isinstance(params, dict):
        return {"classification": "unsupported", "reason": "audit_shape_not_allowlisted",
                "listing_id": listing_id, "source_hash": source_hash}

    # Require enough structured input for the deterministic skill. We do not
    # extract or execute command strings from the listing's prose.
    ready = {
        "gate-probe": isinstance(target.get("blocked_tokens"), list),
        "leak-probe": bool(target),
        "rail-audit": isinstance(target.get("awards"), list) and isinstance(target.get("receipts"), list),
    }[skill]
    if not ready:
        return {"classification": "unsupported", "reason": "audit_input_incomplete",
                "listing_id": listing_id, "source_hash": source_hash}
    return {"classification": "supported", "reason": "bounded_deterministic_audit",
            "listing_id": listing_id, "source_hash": source_hash,
            "skill": skill, "target": target, "params": params}


class OpportunityRunner:
    TERMINAL = frozenset({"submitted", "submission_queued", "submission_uncertain", "seal_uncertain", "unsupported", "project_required"})

    def __init__(self, settings, db, client, executor, publisher=None, sealer=seal_artifact):
        self.settings, self.db, self.client = settings, db, client
        self.executor, self.publisher, self.sealer = executor, publisher, sealer

    def _key(self, evaluation):
        return f'opportunity:{evaluation.get("listing_id", "invalid")}:{evaluation["source_hash"]}'

    def _save(self, key, state, status, **updates):
        state = {**state, **updates, "status": status}
        self.db.set_setting(key, state)
        return state

    def build_artifact(self, listing, evaluation):
        binding = {
            "listing_id": evaluation["listing_id"],
            "source_condition": str(listing.get("condition", "")),
            "source_hash": evaluation["source_hash"],
            "source_payload_hash": listing.get("payload_hash"),
        }
        return run_skill(evaluation["skill"], evaluation["target"], evaluation["params"],
                         Path(self.settings.data_dir) / "artifacts", binding=binding)

    def process(self, listing):
        evaluation = evaluate_opportunity(listing)
        key = self._key(evaluation)
        state = self.db.get_setting(key, {})
        if state.get("status") in self.TERMINAL:
            return {**state, "classification": "already_attempted", "reason": "terminal_attempt_exists"}
        if not state:
            public_evaluation = {k: v for k, v in evaluation.items() if k not in {"target", "params"}}
            self.db.log("opportunity", public_evaluation)
            state = self._save(key, public_evaluation, evaluation["classification"])
        if evaluation["classification"] != "supported":
            return state

        artifact = state.get("artifact")
        if not artifact:
            artifact = self.build_artifact(listing, evaluation)
            state = self._save(key, state, "artifact_ready", artifact=artifact)

        if not artifact.get("public_url"):
            if self.publisher is None:
                return self._save(key, state, "publish_failed", reason="publisher_unavailable")
            try:
                artifact = self.publisher.publish(artifact)
            except Exception as exc:
                self.db.log("opportunity_error", {"listing_id": evaluation["listing_id"],
                            "stage": "publish", "error_type": type(exc).__name__})
                return self._save(key, state, "publish_failed", artifact=artifact)
            state = self._save(key, state, "published", artifact=artifact)

        if artifact.get("seal_id") is None:
            label = f'listing-{evaluation["listing_id"]}'
            try:
                sealed = self.sealer(self.client, self.settings, artifact["hash"], label)
            except (FileNotFoundError, ValueError) as exc:
                self.db.log("opportunity_error", {"listing_id": evaluation["listing_id"],
                            "stage": "seal", "error_type": type(exc).__name__})
                return self._save(key, state, "seal_failed", artifact=artifact)
            except Exception as exc:
                # A transport failure after a POST has an unknown outcome. Never
                # repeat the write automatically.
                self.db.log("opportunity_error", {"listing_id": evaluation["listing_id"],
                            "stage": "seal", "error_type": type(exc).__name__, "uncertain": True})
                return self._save(key, state, "seal_uncertain", artifact=artifact)
            artifact = {**artifact, "seal_id": sealed.get("id")}
            state = self._save(key, state, "sealed", artifact=artifact)

        if not state.get("artifact_logged"):
            self.db.log("artifact", artifact)
            state = self._save(key, state, "sealed", artifact=artifact, artifact_logged=True)

        reference = (f'{artifact["public_url"]} sha256:{artifact["hash"]} '
                     f'commit:{artifact["commit"]} seal:{artifact["seal_id"]}')
        result = self.executor.dispatch(Intent(action="submit", listing_id=evaluation["listing_id"],
                                               artifact=reference,
                                               note="Deterministic listing-specific audit; see evidence and limitations."))
        result_status = result.get("status")
        if result_status == "sent":
            return self._save(key, state, "submitted", submission=result.get("response"))
        if result_status == "uncertain":
            return self._save(key, state, "submission_uncertain")
        if result_status == "queued":
            return self._save(key, state, "submission_queued", queue_id=result.get("queue_id"))
        return self._save(key, state, "submission_failed", submission_result=redact(result))
