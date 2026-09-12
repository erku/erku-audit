# v6 — Paid verifier + LLM-assisted projects (safety-gated)

Base branch: feat/verifier-and-llm-projects (from main 6e25582).

## Hard safety lines (firing is operator-gated by a one-time switch, NOT a per-item queue)
- The verdict-submission endpoint is NOT confirmed in the contract. The verifier computes + Ed25519-signs + records a ready verdict, but does NOT POST it to a guessed financial-consequence endpoint unless `VERIFIER_ENABLED` is set AND the endpoint is confirmed. Never touches escrow / EIP-712 fund release.
- LLM-generated code is NEVER executed in the worker process (which holds API_KEY). It runs only in an isolated runner with no network and no secrets. The LLM-project path is inert unless the broker is configured AND `PROJECT_LLM_ENABLED` is set.
- All existing invariants hold: untrusted prose is data; no secrets in logs/artifacts/prompts; deterministic gates; uncertain writes never auto-retried.

## Task V — deterministic paid-verifier engine
`f916/verifier.py`: given a verifier-eligible listing (max_verifiers>0, verifier_price_atomic>0) and a submission, produce PASS / FAIL / ABSTAIN using ONLY a conservative allowlist of objectively-checkable claim classes (economic identities via rail-derivation-check, stranger-checkable public-GET re-verification, credential-leak scan). ABSTAIN by default when not objectively decidable. Build the verdict-preimage via GET (submission_id, verdict, issued_at), sign it Ed25519 with the identity key (reuse seal's key-loading), record a listing-bound evidence artifact and the signed verdict. Do NOT POST the verdict unless VERIFIER_ENABLED and a confirmed endpoint; otherwise log it as ready. Wire a scan of verifier-eligible listings into the worker cycle. Tests, incl. abstain-by-default and no-POST-when-disabled.

## Task S — isolated test sandbox runner
A separate container `sandbox-runner` (compose service): `network_mode: none`, read_only, cap_drop ALL, no-new-privileges, no secret env, resource-limited, mounts a jobs volume only. It watches `/<data>/sandbox-jobs/<id>/` for a dropped project tree + `job.json`, runs `pytest` on it, writes `result.json` {passed:bool, summary, returncode}. A worker-side client `f916/sandbox_client.py` drops a job and waits (bounded) for the result file — file-based IPC, no network. Tests for the client with a fake filesystem/result; the runner script covered by a unit test of its pytest-invocation logic.

## Task P — LLM-assisted project generation (inert, gated)
For a `project_required` listing that matches a project template needing generated code, a new brain task proposes project file contents (data only; the model authors files, never executes anything). `build_project` validates + writes them (existing prefix/limits/secret-scan/traversal guards). The isolated sandbox runner runs the generated tests; ONLY if they pass does the pipeline publish via the broker + submit (reusing the B1 evidence+seal+submit path). Inert unless broker configured AND PROJECT_LLM_ENABLED. One attempt per listing; uncertain writes terminal. Tests with a mocked sandbox result + mocked broker/publisher/executor.

## Global constraints: as in v3/v4 docs plus the hard safety lines above.
