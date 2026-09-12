# v4 — Reach, learning, autonomous projects

Date: 2026-09-12. Base branch: feat/reach-and-learning (from main 275e4a7).

## Goal
Raise the agent's real effectiveness on the live 1F916 rail without a human per-item queue, keeping every hard invariant (untrusted prose never executes; secrets/creds never reach LLM/artifact/log/worker; submissions are listing-specific, sealed, public; repos public/prefixed/quota-bounded/never-deleted; uncertain writes never auto-retried).

## Task A1 — Curated bounty templates + rail self-report
Widen the `supported` path beyond a structured `audit` field via an OPERATOR-CURATED allowlist of bounty templates. Matching uses only structured signals (funder equality + casefolded title substrings) — a classifier signal, never executing prose. A matched template deterministically builds a read-only, listing-bound job from the listing's OWN already-fetched public fields. Ship a `rail-report` skill and a `rail-state-self-report` template that quotes a listing's public economic/lifecycle/settlement fields and emits a BID/CAUTION/SKIP verdict derived only from those fields. Flows through the existing publish→seal→submit pipeline. No prose is parsed into parameters in v1.

## Task C1 — Outcome feedback loop
Close the loop: match our own submissions (handle == settings.handle) to a listing's awards, classify outcome (submitted/awarded/paid/declined) idempotently, log `outcome` events, and accumulate real earned USD (paid USDC awards) into `reward_metrics.outside_funded_earnings_usd` so the existing weekly reward/tuner reflects reality. Wired into maintenance().

## Task B1 — Autonomous project pipeline (no human queue) + broker hardening
Harden the broker (create_branch primitive; put_file carries the existing blob sha so re-publish is idempotent). Add a deterministic project qualification policy for `project_required` opportunities and a worker->broker client, so a qualifying opportunity is auto-built (builder), auto-published (broker: prefixed public repo + manifest) and auto-submitted — gated ONLY by deterministic policy (allowlisted project template, quota, one-repo-per-listing, passing generated tests, dedup) and the broker's own limits. NO human queue. Goes live only once the operator has bootstrapped the broker credential (a one-time infra step, not a per-item approval); until then the path is inert.

## Global constraints
As in v3 (see docs/1f916-agent-operacje-v3.md). Additionally: the template allowlist is the trust boundary for A1; the project qualification policy + broker limits are the trust boundary for B1. The controller (not the live agent) never itself fires a first irreversible public action, and never handles the gh credential.
