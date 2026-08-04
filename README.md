# PaySentry Core

[![tests](https://github.com/vaibhavshiroorkar/paysentry-core/actions/workflows/ci.yml/badge.svg)](https://github.com/vaibhavshiroorkar/paysentry-core/actions/workflows/ci.yml)

A payment service for merchants. Merchants take card payments, capture or void them, and refund them. Money moves exactly once, even when clients retry, requests race, and transfers cross in opposite directions.

Built with FastAPI, PostgreSQL (asyncpg, raw SQL) and Redis.

- Payment lifecycle, card handling and per-merchant idempotency: [docs/design/payments.md](docs/design/payments.md)
- Gateway connectors, timeouts and recovery: [docs/design/gateways.md](docs/design/gateways.md)
- The ledger underneath: [Design decisions](#design-decisions) below

## Guarantees

Each guarantee has a test that fires concurrent requests at a real Postgres instance.

| Guarantee | How | Test |
|---|---|---|
| A retried request never charges twice | `Idempotency-Key` claimed with `INSERT ... ON CONFLICT DO NOTHING` inside the payment transaction | 50 concurrent copies of one request: 1 transfer, 49 replays |
| No account goes below zero under concurrency | Both account rows locked before the balance check | 50 concurrent spends of 30 from a balance of 100: exactly 3 succeed |
| Opposite transfers never deadlock | Rows locked in id order with `FOR NO KEY UPDATE` | 100 concurrent A->B and B->A transfers: all succeed |
| Every balance can be proven | Append-only double-entry ledger plus a reconciliation check | Every test ends by reconciling the whole ledger |
| One client cannot flood the service | Token bucket per merchant or paying account, in one Redis Lua script | Burst past capacity gets 429 with `Retry-After` |
| A payment is captured at most once | Every transition is `UPDATE ... WHERE status = <expected>` | 20 concurrent captures on one payment: exactly 1 succeeds |
| Refunds never exceed the capture | Pending refunds reserve their amount under the payment's row lock | 20 concurrent refunds of 100 on a 1000 capture: exactly 10 succeed |
| A retry during a slow gateway call is safe | Keys stay `in_progress` until the outcome is stored | A duplicate sent mid-flight gets `409 request_in_progress` |
| A gateway timeout never loses or doubles a charge | Every call carries our reference; a recovery job looks it up or re-sends it | Timeouts and process crashes on every operation, settled exactly once |
| The state machine has no gaps | Model-based test of random capture, void and refund sequences | 30 seeded sequences match a Python model step by step |
| Card numbers are never stored | Only brand and last 4 kept; validation errors do not echo input | Every stored row is searched for each test card number |

## Design decisions

**Money is an integer.** Amounts are `bigint` minor units (paise, cents). Floats cannot represent 0.10 exactly, and decimal columns still need careful handling in Python. The API rejects `10.5` and `"10"`.

**The idempotency key is claimed, not checked.** Checking first (`SELECT` the key, then `INSERT`) has a gap: two retries arriving together both see "no row" and both charge. Here the request inserts the key as its first step, inside the same transaction that moves the money. If another transaction holds the same key uncommitted, Postgres blocks the second insert until the first commits. The second request then reads the stored response and replays it with `Idempotent-Replayed: true`.

**Request errors and business outcomes are different.** An unknown account or a wrong currency rolls the transaction back, so the key stays free for a corrected request. Insufficient funds is a real outcome: it commits as `FAILED`, and every retry with that key gets the same 422. A key reused with a different body gets 422 `idempotency_key_reused`, because the server stores a SHA-256 of each request.

**Locks are taken in a fixed order, and they are the right strength.** A transfer locks both accounts in one statement, ordered by id. So A->B and B->A both queue on the same first row. The lock is `FOR NO KEY UPDATE`, not `FOR UPDATE`. The `INSERT` into `transfers` has foreign keys to both accounts, so Postgres takes a `KEY SHARE` lock on each account row. `FOR UPDATE` conflicts with `KEY SHARE`, so two concurrent transfers deadlock: each holds a `KEY SHARE` the other needs to get past. Swapping the lock in this repo to `FOR UPDATE` makes the concurrency tests fail with `DeadlockDetectedError`. Removing the lock makes the double spend test fail.

**The ledger is the source of truth.** Each successful transfer writes two `ledger_entries` rows that sum to zero. A trigger rejects `UPDATE` and `DELETE` on that table. `accounts.balance_minor` is a cached total. Top-ups come from a per-currency `system:funding` account, so every currency nets to zero. `GET /internal/reconciliation` checks all three rules in one `REPEATABLE READ` snapshot:
- each balance equals the sum of its entries;
- each transfer's entries sum to zero;
- each currency sums to zero.

**The rate limiter fails open.** If Redis is unreachable, payments keep flowing and a warning is logged. Rejecting every payment because a protective layer is down is the worse failure for a payments service. Set `RATE_LIMIT_FAIL_OPEN=false` to return 503 instead. The token bucket allows short bursts, unlike a fixed window, which lets through twice the limit across a window boundary. It reads Redis server time, so app servers with skewed clocks agree.

**Transfers fit in one transaction; payments do not.** A transfer makes no external call, so it runs in one short transaction. A payment calls a card gateway, and holding a transaction open across a network call would hold row locks for as long as the gateway takes. Payments therefore commit an in-flight state first, call the gateway, and record the outcome in a second transaction. [docs/design/payments.md](docs/design/payments.md#the-three-step-pattern) explains the pattern.

## API

Merchant routes take `Authorization: Bearer sk_...`. Every `POST` that changes money takes an `Idempotency-Key` header.

| Method | Path | Notes |
|---|---|---|
| `POST` | `/payments` | `{amount_minor, currency, capture_method, card}`; `capture_method` is `automatic` or `manual` |
| `GET` | `/payments/{id}` | Includes the payment's refunds |
| `POST` | `/payments/{id}/capture` | Optional `{amount_minor}` for a partial capture |
| `POST` | `/payments/{id}/void` | Releases an authorization |
| `POST` | `/payments/{id}/refunds` | `{amount_minor}`; partial refunds allowed |
| `GET` | `/balance` | The merchant's balance per currency |

Admin routes take `X-Admin-Key` (`ADMIN_API_KEY`, `dev-admin-key` in development).

| Method | Path | Notes |
|---|---|---|
| `POST` | `/merchants` | `{name}`; returns the secret key once |
| `POST` | `/accounts` | `{owner_id, currency}`; currency is `INR` or `USD` |
| `GET` | `/accounts/{id}` | Balance in minor units |
| `GET` | `/accounts/{id}/entries?limit=50` | Ledger history, newest first |
| `POST` | `/accounts/{id}/top-ups` | `{amount_minor}`; needs `Idempotency-Key` |
| `POST` | `/transfers` | `{from_account_id, to_account_id, amount_minor, currency}`; needs `Idempotency-Key` |
| `GET` | `/transfers/{id}` | |
| `GET` | `/internal/reconciliation` | `{ok, mismatches}` |
| `POST` | `/internal/recovery/run?older_than_sec=60` | Run one recovery pass now. It also runs every `RECOVERY_INTERVAL_SEC` |
| `GET` | `/health` | Postgres and Redis status |

Status codes for payments are listed in [docs/design/payments.md](docs/design/payments.md#status-codes). For transfers: 201 success, 400 missing key, 401 missing admin key, 403 drawing from a system account, 404 unknown account, 422 validation error, insufficient funds, currency mismatch or reused key, 429 rate limited, 503 dependency down.

Interactive docs are at `http://localhost:8000/docs` while the server runs.

## Run it

Needs Docker and [uv](https://docs.astral.sh/uv/).

```sh
docker compose up --build
```

If you ran an earlier version, reset the database once with `docker compose down -v`. The schema file only adds what is missing.

Try a payment from a second terminal (bash). Test card numbers are listed in [docs/design/payments.md](docs/design/payments.md#test-cards).

```sh
KEY=$(curl -s localhost:8000/merchants -H 'X-Admin-Key: dev-admin-key' \
  -H 'content-type: application/json' -d '{"name":"Acme"}' | jq -r .api_key)
AUTH="Authorization: Bearer $KEY"

# Authorize 500 rupees without capturing.
P=$(curl -s localhost:8000/payments -H "$AUTH" -H 'Idempotency-Key: order-1' \
  -H 'content-type: application/json' \
  -d '{"amount_minor":50000,"currency":"INR","capture_method":"manual",
       "card":{"number":"4242424242424242","exp_month":12,"exp_year":2030,"cvc":"123"}}' | jq -r .id)

# Capture 400, then refund 150. Run either twice: the retry is replayed, not repeated.
curl -s localhost:8000/payments/$P/capture -H "$AUTH" -H 'Idempotency-Key: cap-1' \
  -H 'content-type: application/json' -d '{"amount_minor":40000}'
curl -s localhost:8000/payments/$P/refunds -H "$AUTH" -H 'Idempotency-Key: ref-1' \
  -H 'content-type: application/json' -d '{"amount_minor":15000}'

curl -s localhost:8000/balance -H "$AUTH"
curl -s localhost:8000/internal/reconciliation -H 'X-Admin-Key: dev-admin-key'
```

## Test it

The tests need the Postgres and Redis containers. They create a separate `paysentry_test` database and use Redis database 1.

```sh
docker compose up -d db redis
uv run pytest -v
```

## Layout

```
app/
  payments.py     payment lifecycle: create, capture, void, refund
  connectors/     one connector per gateway API; classifies every outcome
  recovery.py     settles operations whose gateway outcome is unknown
  cards.py        card validation (Luhn, expiry), never exposes the number
  idempotency.py  per-merchant idempotency keys
  auth.py         merchant API keys, admin key
  ledger.py       post_transfer(): claim key, lock rows, post entries
  ratelimit.py    token bucket Lua script
  reconcile.py    ledger and payment consistency checks
  schema.sql      tables, constraints, append-only trigger
  routes/         HTTP layer
gateway/
  app.py          mock card gateway, two API flavours, failure controls
tests/
  test_concurrency.py          transfers: duplicate retries, double spend, deadlock
  test_payment_concurrency.py  racing captures, voids and refunds
  test_payment_model.py        model-based test of the payment state machine
  test_recovery.py             timeouts and crashes on every operation
```

## History

`prototype/` holds the first version of PaySentry (January to February 2026), a payment fraud detection service built on FastAPI and SQLAlchemy. The current code is a rebuild from October 2026 that focuses on ledger correctness under concurrency.

## Roadmap

- Done: merchants, payment lifecycle, per-merchant idempotency, refunds.
- Done: mock gateways with two API styles, outcome classification, recovery of unknown outcomes.
- Routing: success-rate routing across gateways, a circuit breaker and failover.
- Webhooks through a transactional outbox, with retries and HMAC signatures.
- Load tests with p50 and p99 latency.
