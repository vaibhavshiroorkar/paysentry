# Testing and extending PaySentry

The tests exercise the real Postgres locking and Redis scripts used by the application.
Gateway apps run in process through HTTPX transports, which still exercise their HTTP contracts.
Fault wrappers simulate failures before or after the gateway acts.

## Run tests

```powershell
docker compose up -d db redis
uv sync --frozen
uv run python -m pytest -q
```

Use dedicated disposable test data.
The session fixture creates `paysentry_test` if missing.
Application fixtures drop and recreate its public schema and clear Redis database 1.
Never point the tests at an application or shared database.

For alternate dedicated services, set both test connection variables in the terminal:

```powershell
$env:TEST_DATABASE_URL = 'postgresql://paysentry:paysentry@localhost:15432/paysentry_test'
$env:TEST_REDIS_URL = 'redis://localhost:16379/1'
uv run python -m pytest -q
```

The services must already exist on those alternate ports.
These variables affect test fixtures, rather than the normal application settings.
Remove them after the alternate environment is no longer needed:

```powershell
Remove-Item Env:TEST_DATABASE_URL -ErrorAction SilentlyContinue
Remove-Item Env:TEST_REDIS_URL -ErrorAction SilentlyContinue
```

The database setup fixture is session-wide and automatic.
Even an isolated card-validation test currently requires the test Postgres connection.
Run one failing test with `-x --tb=short` while diagnosing a setup failure.

## Test map

| Tests | Behavior protected |
| --- | --- |
| [test_transfers.py](../tests/test_transfers.py) | Accounts, top-ups, balanced transfers, funds checks, validation and reserved owners |
| [test_idempotency.py](../tests/test_idempotency.py) | Transfer replay, changed-body rejection and replayed failed outcomes |
| [test_concurrency.py](../tests/test_concurrency.py) | Duplicate requests, competing spends and opposite-transfer deadlocks |
| [test_ratelimit.py](../tests/test_ratelimit.py) | Burst limits, account scope, refill, fail-open and fail-closed behavior |
| [test_reconcile.py](../tests/test_reconcile.py) | Clean accounting, deliberate mismatches and append-only entry protection |
| [test_merchants.py](../tests/test_merchants.py) | Hashed merchant keys, initial balances, merchant/admin separation and public health |
| [test_idempotency_store.py](../tests/test_idempotency_store.py) | Merchant-scoped claims, in-progress conflicts and completed replay |
| [test_cards.py](../tests/test_cards.py) | Luhn, brand, expiry, CVC, redaction and safe fingerprints |
| [test_payments.py](../tests/test_payments.py) | Payment creation, identity, input safety, replay and merchant limits |
| [test_payment_lifecycle.py](../tests/test_payment_lifecycle.py) | Capture, void, partial refunds, invalid transitions and ownership |
| [test_payment_concurrency.py](../tests/test_payment_concurrency.py) | Capture races, capture versus void, refund reservations and shared clearing |
| [test_payment_model.py](../tests/test_payment_model.py) | Seeded random operation sequences checked against an independent model |
| [test_gateway_mock.py](../tests/test_gateway_mock.py) | Alpha and beta contracts, reference reuse, money conversion and failure controls |
| [test_connectors.py](../tests/test_connectors.py) | Normalized outcomes, connection errors, timeouts, dropped replies and beta decimals |
| [test_payment_gateways.py](../tests/test_payment_gateways.py) | End-to-end gateway lifecycles, pending outcomes and reservation release |
| [test_recovery.py](../tests/test_recovery.py) | Crashes, lookups, leases, competing finishers, missing gateways and background passes |
| [test_routing.py](../tests/test_routing.py) | Eligibility, circuits, safe failover, Redis fallback and routing through the payment API |
| [test_contextual.py](../tests/test_contextual.py) | Features, learning, decay, exploration, finite scores and atomic observations |

The [shared fixtures](../tests/conftest.py) build an application with isolated state and mock transports.
The fixture disables the periodic recovery loop unless a test enables it.
It uses high rate limits and priority routing by default so unrelated behavior stays predictable.
At teardown, clients check reconciliation unless a test deliberately disables that check to inspect corruption.

[helpers.py](../tests/helpers.py) contains ledger and merchant request helpers.
[payment_helpers.py](../tests/payment_helpers.py) contains payment lifecycle requests.
[transports.py](../tests/transports.py) contains network and crash injection wrappers.

## Focused checks

```powershell
uv run python -m pytest tests/test_concurrency.py tests/test_payment_concurrency.py -q
uv run python -m pytest tests/test_recovery.py -q
uv run python -m pytest tests/test_routing.py tests/test_contextual.py -q
uv run python -m pytest tests/test_payment_lifecycle.py -x --tb=short
```

Model-based testing uses fixed seeds and reports the seed and operation step on a mismatch.
Keep failures reproducible before broadening the random input range.

## Extend one behavior at a time

For a payment transition, describe its allowed source states, pending state, final state, and ledger effect first.
Then test the normal result, a same-key retry, a changed request, and two competing operations.
Include gateway uncertainty and recovery if the operation leaves the database.

For a new connector, implement the [Connector protocol](../app/connectors/base.py).
Define which transport failures prove no processing and which leave an unknown result.
Test stable references, lookup behavior, business declines, malformed responses, and money conversion.
Preserve capture, void, and refund affinity to the original gateway.

For a routing change, test eligibility and circuit safety before comparing rankings.
Keep declines distinct from technical errors.
Verify Redis fallback and request replay when every circuit is open.
Use the [routing simulation](../scripts/simulate_routing.py) to compare behavior under changing traffic.

## Simulations and performance claims

The script compares fixed priority, success-rate routing, and contextual routing under controlled gateway behavior.
It uses `paysentry_sim` and Redis database 2 and resets that state between setups.
Run it only against disposable local data.

```powershell
uv run python scripts/simulate_routing.py 100
```

Use a positive multiple of 10 for payments per phase.
The segment scenario uses twice that count.
Every setup checks reconciliation after its traffic completes.
Output includes success rates and, for outages, gateway calls per payment.
It does not measure production throughput, tail latency, provider reliability, or fraud accuracy.

The [routing design](design/routing.md) explains how to interpret the experiments.
Load tests and operational targets remain separate roadmap work.

## CI

The [CI workflow](../.github/workflows/ci.yml) runs on pushes and pull requests.
It starts Postgres 16 and Redis 7, runs `uv sync --frozen`, and executes pytest.
It does not currently run formatting, static type checks, security scans, or load benchmarks.
New checks should target a real gap and stay separate from financial correctness claims.
