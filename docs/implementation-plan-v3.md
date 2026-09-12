# Autonomous opportunity and project pipeline

Date: 2026-09-12

## Goal

Turn `erku-audit` from a monitor with one repeatable self-audit into an autonomous, evidence-first participant that can evaluate opportunities, produce listing-specific evidence, build bounded public projects, and publish or submit only verifiable work.

## Global constraints

- Ollama endpoint remains local and the model remains `deepseek-v4-flash:cloud`.
- Untrusted community text never becomes executable instructions.
- Secrets and broad GitHub credentials never enter an LLM prompt, artifact, log, or the worker container.
- A listing submission must reference evidence produced specifically for that listing, its immutable commit, SHA-256 hash, and seal when available.
- New repositories are public, use the `erku-1f916-` prefix, are created only for a qualified listing/grant/tool, and are never deleted automatically.
- The existing `erku/erku-audit` repository remains the append-only home for small audit evidence.
- Every external action remains subject to existing idempotency and platform action limits.

## Task 1: listing and bounty pipeline

Add a deterministic opportunity evaluator and runner. It records why a listing is supported, unsupported, already attempted, or requires a project. Supported audit jobs produce an artifact containing `listing_id`, the source condition, evidence, reproduction instructions, limitations, and hash. The worker publishes and seals this evidence before constructing a submission. Unsupported work is never represented as completed.

Tests cover supported classification, unsupported classification, listing-specific binding, deduplication, publish-before-submit ordering, and failure behavior.

## Task 2: rotating audit program

Replace the single daily self-redteam with a deterministic rotation across meaningful checks. Publish only when evidence content changes or a listing-specific run exists. Store the audit type and input fingerprint so repeated checks do not create noise. Keep self-redteam, and add safe protocol/rail and data-leak checks when their required inputs exist.

Tests cover rotation, stable hashes, no duplicate publication, and inconclusive results when inputs are insufficient.

## Task 3: response cadence and recovery

Align ordinary triage with the documented hourly cadence while retaining a shorter urgent path. Persist retry state for Ollama rate-limit responses and resume automatically after the supplied reset/retry time, with bounded fallback retries when the service supplies no reset time. Expose the next eligible analysis time in the dashboard.

Tests cover hourly throttling, urgent throttling, retry-after parsing, persisted recovery, and dashboard status.

## Task 4: bounded project builder

Create a deterministic project builder with allowlisted templates for Python tools and static reports. It accepts a normalized project specification, writes only beneath its assigned workspace, rejects symlinks/path traversal/secrets, adds README, license, tests, machine-readable provenance, and produces a validated manifest. It must not execute model-generated shell commands.

Tests cover name normalization, prefix enforcement, traversal and symlink rejection, file/size limits, secret scanning, reproducible output, and template validation.

## Task 5: least-privilege GitHub broker

Add a separate internal service that owns the GitHub credential and exposes a narrow authenticated API to the worker. It can create a limited number of public prefixed repositories and publish validated project manifests/files. It cannot delete repositories, alter unrelated repositories, make repositories private, or accept arbitrary git commands. The worker receives only a broker credential; the LLM receives neither credential.

Add an operator bootstrap script that copies the existing authenticated `gh` token into an ignored Docker secret file without printing it. Do not create a repository until a real opportunity passes the project qualification policy.

Tests cover authentication, prefix and visibility rules, quotas, idempotency, payload bounds, denied operations, and mocked GitHub API calls.

## Task 6: integration, dashboard, and deployment

Integrate opportunity and project state into the dashboard, run the full local and container test suites, perform a security review, deploy the updated Compose stack, verify health and live recovery behavior, commit and push to `main`, and document operational behavior.
