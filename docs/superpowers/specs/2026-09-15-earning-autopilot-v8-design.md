# Earning Autopilot v8 — Design

Date: 2026-09-15

## Objective

Increase `erku-audit`'s chance of earning verifiable rewards without weakening its safety boundary. The agent should discover and rank live opportunities, execute only supported and reproducible work, create public GitHub repositories named with the `1f916_` prefix when a repository is the appropriate artifact, and propose rather than deploy newly generated capabilities.

## Current evidence

The worker is active and builds reputation, but its current opportunity funnel does not convert. In the latest audit it had evaluated 29 opportunities and classified every one as unsupported, project-required, or invalid, while outside-funded earnings remained zero. The live market contains higher-value work that fits adjacent capabilities: reproducible public-data analysis, source research, security review, and small code artifacts.

The repository already contains useful foundations: a market scanner, deterministic audit skills, a bounded project builder, an isolated sandbox runner, a least-privilege GitHub broker, and a self-extension proposal path. The design extends these components instead of introducing an independent executor.

## Safety and authority

- GPT-6-Astra is prohibited for this project and must not appear in project instructions, configuration, automation, or delegated work.
- The agent may automatically create public GitHub repositories only when their names match `^1f916_[a-z0-9][a-z0-9_-]{0,54}$`.
- Automatic repository creation and submission require deterministic qualification, a reproducible artifact, passing tests, a successful static safety scan, a valid manifest, and an available action budget.
- The GitHub credential remains isolated in the broker. The worker and LLM receive only the narrow broker credential; the LLM receives neither credential.
- Generated capability code is never imported, merged, deployed, or executed by the worker. It may run only in the isolated sandbox after static scanning and is stored as a proposal for human review.
- Listing prose, comments, and artifacts are untrusted data. They cannot become commands, tool names, file paths, or executable parameters without allowlisted structural translation.
- Uncertain public writes are terminal and are not automatically repeated.
- Existing action limits, payout signature boundaries, secret redaction, and evidence sealing remain binding.

## Market opportunity model

Introduce a pure `OpportunityScore` calculation over normalized public listing fields and locally measured capability data. It returns a 0–100 score, a decision, component scores, estimated execution cost, and machine-readable reasons.

The score uses:

- reward value, normalized to USD only for known assets; unknown-token rewards are not treated as USD;
- funding confidence: proof-of-funds, prior paid receipts, and funder payment history;
- seat availability and selection shape, including pay-per-valid versus winner-takes-all;
- competition from submissions, bindings, and already recorded awards;
- deadline feasibility with a safety margin;
- capability fit and reproducibility;
- expected model tokens, network work, storage, and wall-clock effort;
- policy and platform-rule compatibility.

Decisions are:

- `skip`: invalid, prohibited, expired, economically negative, unverifiable, or already exhausted;
- `watch`: potentially valuable but currently missing data, a seat, funding confidence, or budget;
- `execute_existing`: an allowlisted recipe can satisfy the condition with bounded work;
- `propose_capability`: valuable and safe in principle, but no reviewed recipe exists.

The worker ranks candidates before any LLM call. Only a bounded number of top candidates may advance per scan. Scoring inputs and rejection reasons are persisted for dashboard inspection and later outcome calibration.

## Reviewed capability families

### Public-data analysis

A recipe describes allowlisted HTTP origins and endpoints, pagination rules, field extraction, deterministic computation, output schema, and reproduction command. It produces a compact repository containing source code, tests, a machine-readable result, and a report. Initial recipes target public API censuses, receipt/batch measurements, and bounded tabular analyses. Large downloads and unbounded crawls are rejected unless a recipe explicitly supplies byte, page, and duration caps.

### Source research

A recipe accepts a structured research brief and produces a citation record from canonical public sources. It enforces domain and publication-date requirements, quote-length limits, duplicate checks, contact-route provenance, and a no-contact rule. Search results are leads only; the artifact must cite and validate primary sources. This family never sends messages or contacts third parties.

### Small code and security artifacts

A recipe builds a narrowly scoped audit, reproduction, parser, or data tool. Code must fit the existing project-size limits, contain tests, run without secrets, and pass both sandbox execution and the static scanner. Tasks needing credentials, deployment into third-party systems, purchases, customer solicitation, or arbitrary shell access are skipped.

## Execution pipeline

The single state machine is:

`observed → scored → qualified → built → tested → scanned → published → sealed → submitted`

Every transition is persisted by listing identity and payload hash. A restart resumes only safe read/build/test stages. Public writes use reservations and idempotency keys; an ambiguous publish, seal, or submission result becomes `uncertain` and stops.

For repository artifacts, the project policy derives a normalized `1f916_<slug>_<listing-id>` name. The builder creates a deterministic manifest and file hashes. The sandbox runs the declared tests without network or secrets. The broker independently revalidates the prefix, paths, limits, manifest, and hashes before creating or updating a public repository. The submission cites the immutable commit and sealed artifact hash.

The broker remains optional at process start, but an opportunity requiring a repository is `watch` rather than permanently rejected when the broker is unavailable. Once valid broker configuration appears, the opportunity may be reconsidered.

## Safe adaptation

Adaptation has two tiers:

1. An auto-template may map a new market class to an existing reviewed recipe when required structured fields can be constructed without interpreting prose. The mapping is bounded, versioned, reversible, and lower priority than operator-curated templates.
2. A missing capability may generate a proposal containing source, tests, intended input/output schema, expected cost, matched opportunities, scan results, and sandbox results. The proposal is shown in the dashboard and requires human review before merge or deployment.

The agent never promotes generated code merely because tests pass. Human approval is a permanent boundary. Failed proposals use bounded retry with cooldown; low-value gaps are re-evaluated cheaply without consuming an LLM call.

## Weekly token conservation

Local model limits are enabled in the deployed configuration. Add a weekly budget policy with three operating bands:

- `normal`: below 60% of the rolling seven-day limit; reviewed opportunity execution and ordinary triage are allowed;
- `economy`: from 60% to 80%; deterministic scans continue, unchanged snapshots are cached, routine triage is reduced, and LLM work requires a positive opportunity score;
- `reserve`: above 80%; the remaining 20% is reserved for urgent inbox safety responses and high-confidence earning work above a configurable minimum score and reward.

Every LLM request declares a task class and estimated input/output tokens before the gate. The gate applies hourly, daily, weekly, and reserve policy together. The estimate includes the configured output ceiling. Cache keys cover normalized prompts and immutable source snapshots. Market scanning, scoring, duplicate detection, payout walks, and template matching use no model.

The dashboard exposes rolling usage, current band, reserved tokens, next reset estimate, blocked request class, and reason. Logs contain counts and reason codes, not prompt bodies.

## Reliability and error handling

- Ollama 429/503 responses honor `Retry-After`, `X-RateLimit-Reset`, and `RateLimit-Reset`; transport errors enter bounded persisted backoff.
- A malformed model response records a typed error, consumes its measured usage, and does not trigger an immediate retry.
- Failure of one listing detail fetch is isolated. The scanner records the listing id and status and continues with other listings.
- Repeated external 5xx responses move an opportunity to `watch` with a retry time instead of marking it permanently unsupported.
- Container health checks cover dashboard readiness, worker heartbeat freshness, and sandbox availability. Broker health is reported separately because it is optional for non-repository work.

## Documentation and project instructions

Create one shared `RTK.md` describing repository architecture, safety invariants, TDD workflow, verification commands, deployment procedure, live-data handling, token discipline, and the GPT-6-Astra prohibition.

- `AGENTS.md` imports `RTK.md` and adds concise Codex-specific instructions.
- `CLAUDE.md` imports `RTK.md` and adds concise Claude Code-specific instructions.
- `.cursor/rules/project-workflow.mdc` applies the same always-on project constraints for compatible editors.
- `README.md` becomes the current operational entry point and links to the current specification.
- `CHANGELOG.md` records released behavior, migrations, configuration changes, and deployment requirements.
- Older implementation plans remain historical and are clearly labeled as superseded where they conflict with the current specification.

## Deployment

Implementation follows test-driven development. Each behavior is first captured by a failing focused test, then implemented minimally, followed by the relevant module suite and the full suite.

Before deployment:

1. all tests pass;
2. `docker compose config --quiet` succeeds for the default and broker profiles;
3. required secrets exist without being printed;
4. GitHub authentication and broker prefix validation pass read-only checks;
5. images are rebuilt from the verified commit.

Deployment uses Compose to recreate the worker, dashboard, sandbox runner, and broker when its credential is available. Post-deploy checks verify image ids, health, worker heartbeat, budget band, a read-only market scan, and absence of crash-loop or repeated write attempts. No live listing submission is created merely as a smoke test.

## Success criteria

- Current tests remain green and new behavior has focused regression tests.
- The deployed containers run images built from the final verified commit.
- Live listing scans produce ranked `skip`, `watch`, `execute_existing`, and `propose_capability` records with explanations.
- At least the three reviewed capability families can qualify representative fixtures and build reproducible artifacts.
- A qualifying repository project uses the `1f916_` prefix and cannot publish before tests, scan, manifest validation, and reservation succeed.
- Generated capability code cannot reach the worker or deployment path.
- Weekly budget state changes behavior at 60% and 80%, preserving the final 20% for qualified urgent or earning tasks.
- README, changelog, current specification, Codex instructions, and Claude Code instructions agree on active features and safety boundaries.
- The agent records conversion metrics from observed listings through submissions and paid outcomes, allowing effectiveness to be measured rather than inferred from activity.

## Explicit non-goals

- Automatic merge or deployment of LLM-generated capabilities.
- Contacting researchers, customers, or other third parties.
- Taking custody of funds or signing wallet messages without the existing human-controlled flow.
- Treating unknown-token nominal amounts as USD.
- Solving listings that require unrestricted shell access, private credentials, purchases, promotion, or unverifiable judgment.
- Guaranteeing earnings or payment by a funder.
