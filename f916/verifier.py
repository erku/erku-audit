"""Deterministic paid-verifier engine.

Some listings pay an independent verifier (max_verifiers>0,
verifier_price_atomic>0) to check a submission and sign a PASS/FAIL verdict.
This module decides PASS/FAIL/ABSTAIN using only a conservative allowlist of
objectively-checkable claim classes, signs a ready verdict with the identity
key exactly like f916.seal.seal_artifact -- but never POSTs it anywhere: the
verdict-submission endpoint is not confirmed in the contract, so a ready
verdict is only ever recorded, never sent. This module never touches escrow
or EIP-712 and never judges subjective quality. ABSTAIN is the default and
the only outcome outside the allowlist below; when in doubt, ABSTAIN.
"""
from __future__ import annotations

import base64
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from cryptography.hazmat.primitives.serialization import load_pem_private_key

from .skills import _leak as _leak_probe
from .skills import _rail_derivation

VERDICTS = ("PASS", "FAIL", "ABSTAIN")


def _positive_int(value):
    """A positive int identity from an int or a numeric string; else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isdigit():
        n = int(value)
        return n if n > 0 else None
    return None


def eligible(listing: dict) -> bool:
    """True iff listing has max_verifiers>0 and verifier_price_atomic a
    positive int."""
    if not isinstance(listing, dict):
        return False
    return (_positive_int(listing.get("max_verifiers")) is not None
            and _positive_int(listing.get("verifier_price_atomic")) is not None)


def _abstain(claim_class, reason):
    return {"verdict": "ABSTAIN", "basis": reason, "claim_class": claim_class}


def _evaluate_economic_identity(listing, submission):
    economics = listing.get("economics") if isinstance(listing, dict) else None
    if not isinstance(economics, dict):
        return _abstain("economic_identity", "No checkable published economic identities on this listing.")
    result = _rail_derivation({"economics": economics, "listing_id": listing.get("listing_id")}, {})
    status = result.get("status")
    if status == "consistent":
        return {"verdict": "PASS", "basis": result.get("summary", "Recomputed published economic identities; consistent."),
                "claim_class": "economic_identity"}
    if status == "findings":
        findings = result.get("findings") or [{}]
        finding = findings[0]
        basis = (f"Recomputed identity {finding.get('identity', '?')}: served {finding.get('served')} "
                 f"!= computed {finding.get('computed')} ({len(findings)} mismatch(es)).")
        return {"verdict": "FAIL", "basis": basis, "claim_class": "economic_identity"}
    return _abstain("economic_identity", result.get("summary", "Economic identities not decidable from supplied fields."))


def _evaluate_stranger_checkable_get(listing, submission, client):
    if client is None:
        return _abstain("stranger_checkable_get", "No client available to re-fetch the public artifact.")
    artifact = submission.get("artifact")
    quoted = submission.get("quoted")
    if not isinstance(artifact, str) or not isinstance(quoted, dict) or not quoted:
        return _abstain("stranger_checkable_get", "Missing artifact URL or quoted fields.")
    parsed = urlsplit(artifact)
    if parsed.scheme != "https" or parsed.netloc != "1f916.ai" or not parsed.path.startswith("/api/"):
        return _abstain("stranger_checkable_get", "Artifact is not a public https://1f916.ai/api/... URL.")
    try:
        fetched = client.get(parsed.path, params=dict(parse_qsl(parsed.query)) or None)
    except Exception:
        return _abstain("stranger_checkable_get", "Re-fetch of the public artifact failed; cannot confirm quoted fields.")
    if not isinstance(fetched, dict):
        return _abstain("stranger_checkable_get", "Re-fetched artifact was not a JSON object.")
    mismatches = sorted(key for key, value in quoted.items() if fetched.get(key) != value)
    if mismatches:
        return {"verdict": "FAIL", "basis": f"Quoted field(s) {mismatches} do not match the live {parsed.path} response.",
                "claim_class": "stranger_checkable_get"}
    return {"verdict": "PASS", "basis": f"Quoted fields for {parsed.path} match the live re-fetch.",
            "claim_class": "stranger_checkable_get"}


def _evaluate_credential_leak(listing, submission):
    acceptance = listing.get("acceptance") if isinstance(listing, dict) else None
    text_fields = {key: value for key, value in submission.items()
                   if key not in ("claim_class", "handle", "id") and isinstance(value, (str, dict, list))}
    result = _leak_probe(text_fields, {})
    if result.get("status") != "findings":
        return _abstain("credential_leak", "No leak evidence found; absence of a leak is not itself verified.")
    if acceptance != "no secrets leaked":
        return _abstain("credential_leak",
                         'Leak evidence found, but listing acceptance is not exactly "no secrets leaked".')
    return {"verdict": "FAIL", "basis": result.get("summary", "Credential-like leak found in submission text."),
            "claim_class": "credential_leak"}


def evaluate_submission(listing: dict, submission: dict, client=None) -> dict:
    """Deterministically decide a verdict for ONE submission, or ABSTAIN.

    Returns {'verdict': one of VERDICTS, 'basis': one-line str citing only
    objectively-checkable facts, 'claim_class': str}. ABSTAIN is the default
    and the ONLY outcome unless the submission's claim falls in the
    conservative allowlist and is objectively decidable from data we can
    re-fetch/recompute. Never raises; never judges subjective quality.
    """
    try:
        if not isinstance(listing, dict) or not isinstance(submission, dict):
            return _abstain("malformed_input", "Listing or submission was not an object.")
        claim_class = submission.get("claim_class")
        claim_class = claim_class if isinstance(claim_class, str) and claim_class else None
        if claim_class == "economic_identity":
            return _evaluate_economic_identity(listing, submission)
        if claim_class == "stranger_checkable_get":
            return _evaluate_stranger_checkable_get(listing, submission, client)
        if claim_class == "credential_leak":
            return _evaluate_credential_leak(listing, submission)
        return _abstain(claim_class or "unknown", "Claim class not in the conservative allowlist.")
    except Exception:
        return _abstain("error", "Unexpected error while evaluating the submission; defaulting to ABSTAIN.")


def build_and_sign_verdict(client, settings, listing_id, submission_id, verdict, issued_at):
    """GET the exact 1f916.verdict.v1 preimage bytes for this verdict, then
    Ed25519-sign them with the identity key (loaded exactly like
    f916.seal.seal_artifact does: identity-ed25519.pem, base64url, strip
    '='). Return {'preimage':.., 'signature':.., 'submission_id':..,
    'verdict':.., 'issued_at':..}. Never logs the key. Only called for
    PASS/FAIL."""
    if verdict not in ("PASS", "FAIL"):
        raise ValueError("Only PASS/FAIL verdicts are signed")
    response = client.get(f"/api/listings/{int(listing_id)}/verdict-preimage",
                           params={"submission_id": submission_id, "verdict": verdict, "issued_at": issued_at})
    preimage = response.get("preimage") if isinstance(response, dict) else None
    if not isinstance(preimage, str) or not preimage:
        raise ValueError("Invalid verdict-preimage response")
    key = load_pem_private_key((Path(settings.data_dir) / "identity-ed25519.pem").read_bytes(), password=None)
    signature = base64.urlsafe_b64encode(key.sign(preimage.encode())).decode().rstrip("=")
    return {"preimage": preimage, "signature": signature, "submission_id": submission_id,
            "verdict": verdict, "issued_at": issued_at}


class Verifier:
    """Restart-safe, conservative verdict engine. Never POSTs a verdict:
    unless settings.verifier_enabled AND a confirmed submission endpoint
    exists (it does not), a signed verdict stays 'ready' in the db."""

    def __init__(self, settings, db, client):
        self.settings, self.db, self.client = settings, db, client

    def _handled_key(self, listing_id, submission_id):
        return f"verifier:{listing_id}:{submission_id}"

    def _verdict_key(self, listing_id, submission_id):
        return f"verifier_verdict:{listing_id}:{submission_id}"

    def process(self, listing: dict) -> dict:
        """If not eligible(listing) -> {'status':'not_eligible'}. Otherwise, for
        each submission in listing['submissions'] not by our own handle and not
        already handled (dedup via a db setting key per (listing_id,submission_id)):
        evaluate_submission; if ABSTAIN, log a 'verifier' event {stage:'abstain'}
        and mark handled. If PASS/FAIL, build_and_sign_verdict, log a 'verifier'
        event {stage:'verdict_ready', verdict, submission_id} and store the signed
        verdict under a db key; mark handled. Do NOT POST the verdict. Restart-safe;
        never auto-retries an uncertain external write; never raises out (guard,
        log 'verifier_error')."""
        listing_id = listing.get("listing_id") if isinstance(listing, dict) else None
        try:
            if not eligible(listing):
                return {"status": "not_eligible"}
            submissions = listing.get("submissions")
            submissions = submissions if isinstance(submissions, list) else []
            own_handle = (self.settings.handle or "").casefold()
            results = []
            for submission in submissions:
                if not isinstance(submission, dict):
                    continue
                handle = submission.get("handle")
                if own_handle and isinstance(handle, str) and handle.casefold() == own_handle:
                    continue
                submission_id = submission.get("id")
                handled_key = self._handled_key(listing_id, submission_id)
                if self.db.get_setting(handled_key) is not None:
                    continue
                try:
                    decision = evaluate_submission(listing, submission, client=self.client)
                except Exception as exc:
                    self.db.log("verifier_error", {"listing_id": listing_id, "submission_id": submission_id,
                                                     "stage": "evaluate", "error_type": type(exc).__name__})
                    continue
                verdict = decision.get("verdict")
                if verdict not in VERDICTS:
                    verdict = "ABSTAIN"
                if verdict == "ABSTAIN":
                    self.db.log("verifier", {"stage": "abstain", "listing_id": listing_id,
                                              "submission_id": submission_id,
                                              "claim_class": decision.get("claim_class"),
                                              "basis": decision.get("basis")})
                    self.db.set_setting(handled_key, {"status": "handled", "verdict": "ABSTAIN"})
                    results.append({"submission_id": submission_id, "status": "abstain"})
                    continue
                try:
                    signed = build_and_sign_verdict(self.client, self.settings, listing_id, submission_id,
                                                      verdict, int(time.time()))
                except Exception as exc:
                    self.db.log("verifier_error", {"listing_id": listing_id, "submission_id": submission_id,
                                                     "stage": "sign", "error_type": type(exc).__name__})
                    continue
                record = {"status": "ready", "verdict": verdict, "basis": decision.get("basis"),
                          "claim_class": decision.get("claim_class"), **signed}
                self.db.set_setting(self._verdict_key(listing_id, submission_id), record)
                self.db.log("verifier", {"stage": "verdict_ready", "listing_id": listing_id,
                                          "submission_id": submission_id, "verdict": verdict})
                self.db.set_setting(handled_key, {"status": "handled", "verdict": verdict})
                results.append({"submission_id": submission_id, "status": "verdict_ready", "verdict": verdict})
            return {"status": "processed", "results": results}
        except Exception as exc:
            self.db.log("verifier_error", {"listing_id": listing_id, "stage": "process",
                                             "error_type": type(exc).__name__})
            return {"status": "error"}
