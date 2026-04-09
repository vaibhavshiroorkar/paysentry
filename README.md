# PaySentry Core

[![tests](https://github.com/vaibhavshiroorkar/paysentry-core/actions/workflows/ci.yml/badge.svg)](https://github.com/vaibhavshiroorkar/paysentry-core/actions/workflows/ci.yml)

A payments ledger API that moves money exactly once, even when clients retry, requests race, and transfers cross in opposite directions.

Built with FastAPI, PostgreSQL (asyncpg, raw SQL) and Redis.

## Guarantees

Each guarantee has a test that fires concurrent requests at a real Postgres instance.

| Guarantee | How | Test |
|---|---|---|
| A retried request never charges twice | `Idempotency-Key` claimed with `INSERT ... ON CONFLICT DO NOTHING` inside the payment transaction | 50 concurrent copies of one request: 1 transfer, 49 replays |
| No account goes below zero under concurrency | Both account rows locked before the balance check | 50 concurrent spends of 30 from a balance of 100: exactly 3 succeed |
| Opposite transfers never deadlock | Rows locked in id order with `FOR NO KEY UPDATE` | 100 concurrent A->B and B->A transfers: all succeed |
| Every balance can be proven | Append-only double-entry ledger plus a reconciliation check | Every test ends by reconciling the whole ledger |
| One client cannot flood the service | Token bucket per paying account, in one Redis Lua script | Burst past capacity gets 429 with `Retry-After` |

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

**No PROCESSING state is visible yet.** v0.1 makes no external calls, so the whole posting fits in one short transaction. Once real payment gateways are called, holding a transaction open across a network call is wrong. That version will commit a `PROCESSING` row first and resolve it later.

## API

| Method | Path | Notes |
|---|---|---|
| `POST` | `/accounts` | `{owner_id, currency}`; currency is `INR` or `USD` |
| `GET` | `/accounts/{id}` | Balance in minor units |
| `GET` | `/accounts/{id}/entries?limit=50` | Ledger history, newest first |
| `POST` | `/accounts/{id}/top-ups` | `{amount_minor}`; needs `Idempotency-Key` |
| `POST` | `/transfers` | `{from_account_id, to_account_id, amount_minor, currency}`; needs `Idempotency-Key` |
| `GET` | `/transfers/{id}` | |
| `GET` | `/internal/reconciliation` | `{ok, mismatches}` |
| `GET` | `/health` | Postgres and Redis status |

Status codes: 201 success, 400 missing key, 403 drawing from a system account, 404 unknown account, 422 validation error, insufficient funds, currency mismatch or reused key, 429 rate limited, 503 dependency down.

Interactive docs are at `http://localhost:8000/docs` while the server runs.

## Run it

Needs Docker and [uv](https://docs.astral.sh/uv/).

```sh
docker compose up --build
```

Try it from a second terminal (bash):

```sh
A=$(curl -s localhost:8000/accounts -H 'content-type: application/json' \
  -d '{"owner_id":"alice","currency":"INR"}' | jq -r .id)
B=$(curl -s localhost:8000/accounts -H 'content-type: application/json' \
  -d '{"owner_id":"bob","currency":"INR"}' | jq -r .id)

curl -s localhost:8000/accounts/$A/top-ups -H 'content-type: application/json' \
  -H 'Idempotency-Key: topup-1' -d '{"amount_minor":100000}'

# Run this twice. The second response carries Idempotent-Replayed: true,
# and Alice is charged once.
curl -si localhost:8000/transfers -H 'content-type: application/json' -H 'Idempotency-Key: pay-1' \
  -d "{\"from_account_id\":\"$A\",\"to_account_id\":\"$B\",\"amount_minor\":2500,\"currency\":\"INR\"}"

curl -s localhost:8000/accounts/$A
curl -s localhost:8000/internal/reconciliation
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
  ledger.py       post_transfer(): claim key, lock rows, post entries
  ratelimit.py    token bucket Lua script
  reconcile.py    ledger consistency checks
  schema.sql      tables, constraints, append-only trigger
  routes/         HTTP layer
tests/
  test_concurrency.py   duplicate retries, double spend, deadlock
```

## Roadmap

- Gateway orchestration: mock payment gateways with set latency and failure rates, a visible `PROCESSING` state, success-rate routing, a circuit breaker and failover.
- Webhooks through a transactional outbox, with retries and HMAC signatures.
- Load tests with p50 and p99 latency.
