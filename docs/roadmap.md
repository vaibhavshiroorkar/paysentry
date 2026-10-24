# PaySentry roadmap

Build a payment system whose behavior can be explained, tested, and demonstrated under failure.

The target is a strong portfolio project with a working demo.
A service for real merchants needs a separate launch scope.
The order below is a proposal. Each milestone should deliver something useful on its own.

Keep Python, FastAPI, asyncpg, Postgres, and Redis. Extend the existing application before introducing more services.
Keep `prototype/` as history. New work belongs in the root application.

## Portfolio demo target

A visitor should be able to follow one payment from request to ledger entry.
Use synthetic data throughout the demo.

1. Create a payment and show its state, gateway attempt, and ledger postings.
2. Retry the same request and show the stored response without another charge.
3. Capture part of an authorization, then refund part of the capture.
4. Inject a gateway timeout and show recovery finishing the original operation.
5. Make a gateway unavailable and show safe failover for an operation known not to have processed.
6. Make a webhook receiver fail, then show retry and successful delivery.
7. Run reconciliation and show that payment records and ledger balances agree.

Support a repeatable local demo before hosting it.
The public demo should offer isolated sessions or a read-only walkthrough with controlled failure scenarios.
Provide a short walkthrough, an architecture diagram, and reproducible commands.
Describe measured limits and engineering tradeoffs alongside the successful path.

## Rules for every milestone

- Store money in integer minor units. Balance each currency separately.
- Commit payment state and its ledger postings together.
- Keep gateway calls outside database transactions.
- Preserve merchant isolation, request replay, and the existing lock order.
- Resolve unknown gateway outcomes before attempting another charge.
- Keep card numbers, CVCs, and credentials out of logs, events, and exports.
- Extend the existing concurrency, recovery, and reconciliation tests when behavior changes.
- Add dependencies or infrastructure only when a milestone needs them.

The [payment design](design/payments.md), [gateway design](design/gateways.md), and [routing design](design/routing.md)
describe the behavior that new work must preserve.

## Recommended sequence

| Order | Deliverable | Depends on | Completion check |
| --- | --- | --- | --- |
| 1 | Database migrations and repeatable setup | Existing schema | Upgrade a populated database without losing payments or entries |
| 2 | Merchant webhooks through an outbox | 1 | Committed events survive crashes and failed deliveries |
| 3 | Payment and worker visibility | 2 | Trace a payment through its gateway call, recovery, and webhook |
| 4 | Load and failure benchmarks | 3 | Measure latency, throughput, lock waits, and recovery without ledger drift |
| 5 | Merchant API and key management | 1 | Browse history safely and rotate keys without downtime |
| 6 | Merchant dashboard and local demo | 2, 3, 5 | Complete a payment, refund it, and inspect its delivery history |
| 7 | Isolated hosted demo and operations | 4, 6 | Deploy, restart, restore, and exercise a synthetic payment |

Milestones 2 and 5 can proceed independently once migrations exist.
Public hosting also requires the access controls and secret handling described below.

## 1. Database migrations and repeatable setup

Extend [app/db.py](../app/db.py), [app/schema.sql](../app/schema.sql), and the startup flow in
[app/main.py](../app/main.py). Add versioned migrations before adding more tables.

- Establish a baseline for both empty databases and databases created by the current startup schema.
- Track applied versions and serialize migration execution across app instances.
- Separate schema changes from normal API startup. Define how deployments run migrations.
- Use additive changes first. Document backups and recovery for destructive changes.
- Add a PowerShell quickstart alongside the shell examples in the [README](../README.md).
- Keep `uv sync --frozen`, local setup, Docker setup, and CI consistent.
- Align package and API version reporting before publishing a release.

Acceptance: fresh setup works, repeated setup is safe, and upgrades preserve populated ledger data.
Test concurrent migration attempts and failure partway through a migration.

## 2. Merchant webhooks through a transactional outbox

Extend payment finalization in [app/payments.py](../app/payments.py) and recovery in
[app/recovery.py](../app/recovery.py). Add an event store and delivery worker.

- Write an immutable event in the same transaction as the payment outcome and ledger postings.
- Begin with successful authorization, capture, void, refund, and terminal failure events.
- Assign stable event IDs, payment IDs, merchant IDs, occurrence times, and a payload version.
- Keep replayed API requests and repeated recovery passes from creating duplicate logical events.
- Store merchant webhook endpoints and signing secrets separately from payment records.
- Sign payloads with a timestamp. Document how receivers verify signatures and reject stale requests.
- Deliver with leases, bounded timeouts, backoff, jitter, and a finite retry policy.
- Preserve failed deliveries for inspection and authorized manual redelivery.
- Treat delivery as at least once. Receivers must deduplicate using the stable event ID.
- Restrict endpoint schemes and destinations. Recheck resolved addresses and redirects before delivery.

Acceptance: rollback creates no event; a crash after commit loses no event.
Test worker races, lease expiry, duplicate delivery, receiver timeouts, and signing secret rotation.

## 3. Payment and worker visibility

Extend [app/routes/ops.py](../app/routes/ops.py), gateway connectors, routing, and recovery.

- Add structured logs with request, payment, attempt, refund, and event IDs.
- Redact credentials and card data from normal logs, errors, and tracing attributes.
- Measure request latency, declines, gateway timeouts, routing decisions, and connection pool waits.
- Measure recovery backlog age, worker failures, webhook backlog age, and reconciliation mismatches.
- Keep metric labels bounded. Put individual payment and merchant IDs in logs rather than metric labels.
- Separate process liveness from readiness. Define readiness when Redis is unavailable but fallback works.
- Run scheduled reconciliation and surface mismatches for investigation.
- Record administrative actions, key changes, webhook configuration changes, and manual redeliveries.

Acceptance: one payment can be followed across the API, gateway, recovery, and delivery worker.
Injected failures produce actionable signals without exposing secrets.

## 4. Load and failure benchmarks

Build on the concurrency tests and [scripts/simulate_routing.py](../scripts/simulate_routing.py).
Use dedicated benchmark databases and Redis namespaces.

- Measure independent merchants, one busy merchant, and the shared gateway clearing account separately.
- Include duplicate requests, opposite transfers, refund races, slow gateways, and unknown outcomes.
- Increase concurrency gradually. Record throughput, p50/p95/p99 latency, errors, lock waits, and pool usage.
- Restart API and worker processes between gateway success and local finalization.
- Exercise Redis loss, database disconnects, gateway recovery, and delivery backlog catch-up.
- Measure recovery completion time and webhook delay after an outage.
- Run reconciliation after each scenario and compare gateway operations with local ledger postings.
- Establish a baseline before choosing throughput targets or changing the ledger design.
- Add indexes, pool limits, or concurrency limits only when measurements justify them.
- Consider splitting clearing accounts only if measured lock contention warrants the added complexity.

Acceptance: runs are repeatable and preserve accounting invariants.
Document benchmark commands and methodology. Keep individual run reports out of the repository.

## 5. Merchant API and key management

Extend [app/auth.py](../app/auth.py), [app/routes/merchants.py](../app/routes/merchants.py),
[app/routes/payments.py](../app/routes/payments.py), and [app/routes/accounts.py](../app/routes/accounts.py).

- Add merchant-scoped payment, refund, and event listing with stable cursor pagination.
- Filter payments by state, creation time, currency, and merchant order reference.
- Add bounded metadata and a merchant order reference without weakening request fingerprints.
- Add merchant statements and exports with clear currency and time boundaries.
- Support multiple named API keys, rotation overlap, revocation, scopes, and last-used timestamps.
- Keep administrator permissions separate from merchant permissions.
- Define API error codes, retry guidance, idempotency retention, and behavior after key expiry.
- Add typed response schemas and examples for asynchronous payment outcomes.
- Enforce integer bounds and define supported currencies and their minor units explicitly.

Acceptance: pagination stays stable while new payments arrive.
Test cross-merchant access, revoked keys, overlapping rotation, filtering, and retries across retention boundaries.

## 6. Merchant dashboard and local demo

Build the interface on the merchant API. Choose the frontend stack when this milestone starts.

- Show payment history, payment details, balances, refunds, and ledger-backed statements.
- Show the payment timeline, gateway attempts, pending recovery, and webhook delivery history.
- Support capture, void, and refund with clear amounts and confirmation for money-changing actions.
- Add webhook setup, test delivery, signing secret rotation, and API key management.
- Keep administrator tools behind separate permissions.
- Use server-side sessions or a backend layer so merchant secret keys stay out of browser storage.
- Provide a synthetic checkout with test cards and scripted failure scenarios.
- Seed demo merchants and payments through a repeatable command.
- Cover empty states, pending operations, failures, keyboard navigation, and mobile layouts.

Acceptance: a new reader can run the demo, retry a payment, refund it, and inspect the resulting events.
The dashboard must handle a pending payment without encouraging a second charge.

## 7. Isolated hosted demo and operations

Extend the [Dockerfile](../Dockerfile), [Compose configuration](../docker-compose.yml), and
[CI workflow](../.github/workflows/ci.yml) as needed.

- Separate demo configuration and credentials from local development.
- Reject development secrets in hosted configuration.
- Restrict mock gateway controls, administrator routes, database ports, and Redis access.
- Keep public visitors on synthetic data with limited permissions and resource quotas.
- Add HTTPS, secret injection, persistent storage, database backups, and a tested restore procedure.
- Run containers without root where practical. Add resource limits and graceful shutdown checks.
- Add formatting, static checks, dependency checks, migration checks, and tests to CI.
- Build and tag reproducible release images. Pin infrastructure versions with an update process.
- Add a deployment smoke test and a rollback procedure that accounts for schema compatibility.
- Document how to investigate stuck payments, failed deliveries, and reconciliation mismatches.

Acceptance: deployment and restart preserve state, and a restored backup passes reconciliation.
Keep the hosted demo limited to mock payment processing.

## Further payment-system work

These are separate projects. Pick them when they support the intended use case.

| Area | Work to consider | Prerequisites and proof |
| --- | --- | --- |
| Real gateway sandbox | Add one provider connector, token-based inputs, callback verification, and contract tests | Confirm provider contracts first; prove duplicate requests and unknown outcomes remain safe |
| Incoming gateway events | Verify signatures, deduplicate events, handle reordering, and reconcile callbacks with polling | Durable event inbox; repeated or stale events cannot post money twice |
| Authorization expiry | Track expiration, release holds, and reject capture after expiry | Provider-specific rules and races between capture, expiry, and callbacks |
| Additional capture models | Support multiple captures only for gateways that allow them | Redesign capture records, posting keys, remaining authorization, and reconciliation together |
| Settlement reconciliation | Import gateway settlement records and match gross amounts, fees, net amounts, and adjustments | External references and an exception queue; test missing, duplicate, and late records |
| Fees and reserves | Add fee accounts, pending versus available balances, and reserve releases | Explicit accounting rules; prove conservation and refund behavior |
| Payouts | Add payout requests, reservations, settlement status, and failed-payout reversal | Available balance model and provider contracts; test concurrent refunds and payouts |
| Disputes | Record chargebacks, evidence deadlines, reversals, and merchant exposure | A separate lifecycle and ledger postings; avoid overloading refund records |
| More currencies | Define currency minor units and gateway support | Keep each ledger balanced; treat foreign exchange as a separate accounting design |
| Other payment methods | Add hosted checkout, payment links, bank transfers, or UPI through a provider sandbox | Define asynchronous confirmation and method-specific failure states first |
| Recurring payments | Model reusable tokens, schedules, consent records, and retries | Provider support; test duplicate scheduler runs and cancelled schedules |

Keep direct card handling confined to the existing mock flow.
A real gateway integration should start with provider-hosted collection or tokenization and a separate security review.
Assess real-money launch requirements against current provider, operational, and legal obligations before implementing that scope.

## Routing, fraud, and analysis

The existing contextual router provides a useful base for experiments.
Preserve deterministic routing and safe fallback while testing alternatives.

| Project | First useful scope | Evidence required |
| --- | --- | --- |
| Routing evaluation | Compare priority, success-rate, and contextual strategies using fixed seeds and segment shifts | Approval rate, timeout rate, latency, uncertainty, and recovery cost across repeated runs |
| Cost-aware routing | Add gateway fees and latency to the objective | Show tradeoffs against the current approval-focused baseline |
| Model operations | Version features and model state; detect drift and test cold starts | Safe behavior after Redis loss, partial state, and model upgrades |
| Routing safety | Bound exploration and explain gateway eligibility and selection | Capability filters and unknown-outcome rules always override model scores |
| Fraud rules | Add velocity checks, amount rules, and an inspectable decision record | Test false positives, duplicate requests, rule changes, and manual review outcomes |
| Risk scoring | Compare a simple baseline with a trained model using synthetic or permitted data | Prevent label leakage; report precision, recall, calibration, and false-positive cost |
| Linked-account analysis | Explore shared token, device, or network signals on synthetic data | Explain each flag and test legitimate shared-device cases |
| Payment analytics | Summarize success, failures, refunds, and settlement by merchant and period | Totals reconcile with source records and separate currencies |
| Documentation assistant | Answer questions from versioned project docs with source citations | Reject unsupported answers; keep decisions and money movement outside the assistant |

Fraud detection and routing solve different problems. Evaluate them separately.
Do not add a model until a simpler baseline and an evaluation dataset exist.

## Scope decisions before execution

- Which merchant workflow supplies the example orders and payment amounts.
- Whether real provider sandbox integration matters more than settlement simulation.
- Frontend preference and hosting budget when dashboard work starts.
- Expected traffic and acceptable latency after the benchmark baseline exists.

Start with migrations, then the webhook outbox.
Write a focused implementation plan for each milestone before building it.
