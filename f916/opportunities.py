"""Conservative, restart-safe execution of listing-specific deterministic audits."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from invariants import redact
from .broker_client import BrokerClient
from .builder import build_project
from .models import Intent
from .project_policy import load_project_templates, qualify
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
    # Terminal states for the project-autopilot pipeline (see run_project):
    # a per-listing key attempted exactly once, distinct from TERMINAL above.
    PROJECT_TERMINAL = frozenset({
        "project_unqualified", "project_uncertain", "project_submitted",
        "project_queued", "project_submission_failed", "project_error",
    })

    def __init__(self, settings, db, client, executor, publisher=None, sealer=seal_artifact,
                 broker_client_cls=BrokerClient):
        self.settings, self.db, self.client = settings, db, client
        self.executor, self.publisher, self.sealer = executor, publisher, sealer
        self.broker_client_cls = broker_client_cls
        # Defense in depth: if the broker token is ever accidentally placed in
        # a logged event's text, db.log's redact() will scrub it (redact()
        # ignores falsy entries, so this is a no-op while broker_token is '').
        if settings.broker_token:
            self.db.secrets.append(settings.broker_token)

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
        classification = evaluation["classification"]
        # The project autopilot is inert unless BOTH broker settings are
        # configured; with no broker env, project_required behaves exactly
        # as before (a single terminal, human-review-shaped stop).
        project_active = classification == "project_required" and bool(
            self.settings.broker_url and self.settings.broker_token
        )
        if state.get("status") in self.TERMINAL and not project_active:
            return {**state, "classification": "already_attempted", "reason": "terminal_attempt_exists"}
        if not state:
            public_evaluation = {k: v for k, v in evaluation.items() if k not in {"target", "params"}}
            self.db.log("opportunity", public_evaluation)
            state = self._save(key, public_evaluation, classification)
        if classification == "project_required":
            if project_active:
                return self.run_project(listing, evaluation)
            return state
        if classification != "supported":
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

    def _project_key(self, evaluation):
        return f'project:{evaluation.get("listing_id", "invalid")}:{evaluation["source_hash"]}'

    def run_project(self, listing, evaluation):
        """Autonomous, no-human-queue delivery of a `project_required` listing.

        Only ever invoked by `process` when a broker is configured. Every
        step is checkpointed under a per-listing key (`_project_key`) so a
        listing is attempted once: any not-yet-confirmed remote write
        (broker create/publish, evidence publish, seal, submit) lands in a
        terminal 'uncertain' state and is never retried automatically, and
        any unexpected exception is caught, logged as `project_error`
        (no secrets), and turned into a terminal state rather than crashing
        the opportunity cycle.
        """
        key = self._project_key(evaluation)
        state = self.db.get_setting(key, {})
        if state.get("status") in self.PROJECT_TERMINAL:
            return state
        listing_id = evaluation.get("listing_id")
        try:
            if "spec" not in state:
                templates = load_project_templates()
                existing_count = len(self.db.get_setting("project_repos", []))
                spec = qualify(evaluation, listing, templates,
                                existing_project_count=existing_count,
                                max_projects=self.settings.max_projects)
                if spec is None:
                    self.db.log("project", {"listing_id": listing_id, "stage": "qualify", "status": "unqualified"})
                    return self._save(key, state, "project_unqualified")
                self.db.log("project", {"listing_id": listing_id, "stage": "qualify",
                            "status": "qualified", "name": spec["name"]})
                state = self._save(key, state, "qualified", spec=spec)
            spec = state["spec"]

            if "manifest" not in state:
                workspace = Path(self.settings.data_dir) / "projects"
                manifest = build_project(spec, workspace)
                self.db.log("project", {"listing_id": listing_id, "stage": "build", "name": spec["name"]})
                state = self._save(key, state, "built", manifest=manifest)
            manifest = state["manifest"]

            need_repo = "repo" not in state
            need_publish = "publish_result" not in state
            broker = self.broker_client_cls(self.settings.broker_url, self.settings.broker_token) \
                if (need_repo or need_publish) else None

            if need_repo:
                try:
                    repo = broker.create_repo(spec["name"])
                except Exception as exc:
                    self.db.log("project_error", {"listing_id": listing_id, "stage": "create_repo",
                                "error_type": type(exc).__name__})
                    return self._save(key, state, "project_uncertain")
                repos = self.db.get_setting("project_repos", [])
                if spec["name"] not in repos:
                    self.db.set_setting("project_repos", repos + [spec["name"]])
                self.db.log("project", {"listing_id": listing_id, "stage": "create_repo", "name": spec["name"]})
                state = self._save(key, state, "repo_created", repo=repo)

            if "publish_result" not in state:
                workspace = Path(self.settings.data_dir) / "projects"
                project_dir = workspace / spec["name"]
                # Read back exactly manifest['files'] from the built tree
                # (never the manifest file itself) as the broker payload.
                file_contents = {entry["path"]: (project_dir / entry["path"]).read_text(encoding="utf-8")
                                  for entry in manifest["files"]}
                try:
                    publish_result = broker.publish(spec["name"], manifest, file_contents)
                except Exception as exc:
                    self.db.log("project_error", {"listing_id": listing_id, "stage": "broker_publish",
                                "error_type": type(exc).__name__})
                    return self._save(key, state, "project_uncertain")
                self.db.log("project", {"listing_id": listing_id, "stage": "broker_publish", "name": spec["name"]})
                state = self._save(key, state, "project_published", publish_result=publish_result)

            repo_url = (state.get("repo") or {}).get("html_url", "")

            if "evidence" not in state:
                # Publish the manifest itself as audit evidence to erku-audit
                # (a sealable, listing-bound artifact) -- the project lives in
                # the new repo, the evidence lives in erku-audit, and the
                # submission references both.
                artifacts_dir = Path(self.settings.data_dir) / "artifacts" / spec["name"]
                artifacts_dir.mkdir(parents=True, exist_ok=True)
                manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False)
                                   + "\n").encode("utf-8")
                evidence_path = artifacts_dir / "project.manifest.json"
                evidence_path.write_bytes(manifest_bytes)
                evidence = {
                    "listing_id": listing_id,
                    "source_hash": spec.get("source_hash"),
                    "project_name": spec["name"],
                    "project_repo": repo_url,
                    "hash": hashlib.sha256(manifest_bytes).hexdigest(),
                    "evidence_files": [str(evidence_path)],
                }
                state = self._save(key, state, "evidence_ready", evidence=evidence)
            evidence = state["evidence"]

            if not evidence.get("public_url"):
                if self.publisher is None:
                    return self._save(key, state, "project_uncertain", evidence=evidence)
                try:
                    evidence = self.publisher.publish(evidence)
                except Exception as exc:
                    self.db.log("project_error", {"listing_id": listing_id, "stage": "evidence_publish",
                                "error_type": type(exc).__name__})
                    return self._save(key, state, "project_uncertain", evidence=evidence)
                self.db.log("project", {"listing_id": listing_id, "stage": "evidence_publish", "name": spec["name"]})
                state = self._save(key, state, "evidence_published", evidence=evidence)

            if evidence.get("seal_id") is None:
                label = f'project-{listing_id}'
                try:
                    sealed = self.sealer(self.client, self.settings, evidence["hash"], label)
                except (FileNotFoundError, ValueError) as exc:
                    self.db.log("project_error", {"listing_id": listing_id, "stage": "seal",
                                "error_type": type(exc).__name__})
                    return self._save(key, state, "project_error", evidence=evidence)
                except Exception as exc:
                    # A transport failure after a POST has an unknown outcome.
                    # Never repeat the write automatically.
                    self.db.log("project_error", {"listing_id": listing_id, "stage": "seal",
                                "error_type": type(exc).__name__, "uncertain": True})
                    return self._save(key, state, "project_uncertain", evidence=evidence)
                evidence = {**evidence, "seal_id": sealed.get("id")}
                state = self._save(key, state, "sealed", evidence=evidence)

            if not state.get("evidence_logged"):
                self.db.log("artifact", evidence)
                state = self._save(key, state, "sealed", evidence=evidence, evidence_logged=True)

            evidence_ref = (f'{evidence["public_url"]} sha256:{evidence["hash"]} '
                            f'commit:{evidence["commit"]} seal:{evidence["seal_id"]}')
            reference = f'{repo_url} {evidence_ref}'
            result = self.executor.dispatch(Intent(
                action="submit", listing_id=listing_id, artifact=reference,
                note="Autonomous deterministic project delivered; see repository and evidence."))
            result_status = result.get("status")
            if result_status == "sent":
                return self._save(key, state, "project_submitted", submission=result.get("response"))
            if result_status == "uncertain":
                return self._save(key, state, "project_uncertain")
            if result_status == "queued":
                return self._save(key, state, "project_queued", queue_id=result.get("queue_id"))
            return self._save(key, state, "project_submission_failed", submission_result=redact(result))
        except Exception as exc:
            self.db.log("project_error", {"listing_id": listing_id, "stage": "unexpected",
                        "error_type": type(exc).__name__})
            return self._save(key, state, "project_error")
