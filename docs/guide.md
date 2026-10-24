# PaySentry study guide

For the development sequence, read [Building PaySentry](build-story.md).
Use the [documentation index](README.md) for setup, architecture, API contracts, operations, and the test map.

This guide walks through the whole project in the order I would learn it: the ledger first, then payments, then gateways, then routing, then the tests. Each part says what problem the code solves and how it solves it. It also names the file to read and the test that proves it. Each part ends with a few questions, with answers you can expand.

The design docs ([payments](design/payments.md), [gateways](design/gateways.md), [routing](design/routing.md)) are the reference. This guide is the tour. When the two overlap, the design docs have the full tables and settings.

## Contents

1. [The problem](#1-the-problem)
2. [Run it first](#2-run-it-first)
3. [The map](#3-the-map)
4. [The ledger](#4-the-ledger)
5. [Payments](#5-payments)
6. [Gateways and unknown outcomes](#6-gateways-and-unknown-outcomes)
7. [Routing](#7-routing)
8. [How it is tested](#8-how-it-is-tested)
9. [Startup and configuration](#9-startup-and-configuration)
10. [Exercises: break it on purpose](#10-exercises-break-it-on-purpose)
11. [Glossary](#11-glossary)
12. [Further reading](#12-further-reading)

## 1. The problem

A shop's server asks PaySentry to charge a card 500 rupees. PaySentry charges it, but the network drops the reply. The shop's server never hears back, so it sends the same request again. If PaySentry treats that as a new payment, the customer pays twice.

That is the simplest version of the problem. The harder versions look like this:

- Two copies of the same request arrive at the same millisecond.
- Two requests try to capture the same payment at once.
- Twenty refunds arrive together, and together they ask for more than was captured.
- PaySentry sends a charge to a card gateway and the gateway does not answer within 2 seconds. Did it charge the card or not?
- PaySentry crashes between charging the card and writing down that it did.

Every part of this codebase exists to make one of those cases safe. Read the rest of this guide with that list in mind.

## 2. Run it first

Reading code you have never run is hard. Spend five minutes here first.

```sh
docker compose up --build        # Postgres, Redis, the API and three mock gateways
```

Then follow the curl session in the [README](../README.md#try-a-payment). It creates a merchant, authorizes a payment, captures part of it, refunds part of that, and checks the ledger. Run the capture line twice and look at the `Idempotent-Replayed: true` header on the second answer. Add `-i` to curl to see headers.

Run the tests too:

```sh
docker compose up -d db redis
uv run pytest -v
```

Open `http://localhost:8000/docs` while the API runs. FastAPI lists every route there and lets you call them from the browser.

## 3. The map

### The pieces

```
merchant's server
      |  HTTPS: Authorization: Bearer sk_..., Idempotency-Key: ...
      v
FastAPI app (app/main.py)
  routes/         parse the request, check auth, check the rate limit
  payments.py     the payment lifecycle
  ledger.py       moves money between accounts
  routing.py      picks a gateway       ------> Redis (gateway health, model state, rate limits)
  connectors/     talks to gateways     --HTTP-> mock gateways (gateway/app.py)
  recovery.py     background loop that settles unknown outcomes
      |
      v
PostgreSQL (app/schema.sql)
```

Postgres holds everything that must not be lost: accounts, money, payments and idempotency keys. Redis holds things that are fine to lose or rebuild: rate limit buckets, gateway success windows, circuit breaker state and the routing model. If Redis goes down, payments keep working. They just lose rate limiting and smart routing until it comes back.

### The tables

| Table | One row is | Notes |
|---|---|---|
| `accounts` | A balance in one currency | `balance_minor` is a cached total of the ledger entries |
| `transfers` | One movement of money between two accounts | Also stores the idempotency key and the exact response |
| `ledger_entries` | One side of a transfer | Append-only. A trigger blocks `UPDATE` and `DELETE` |
| `merchants` | A merchant | Stores a SHA-256 of the API key, never the key |
| `idempotency_keys` | One merchant request key | `in_progress` or `completed`, plus the stored response |
| `payments` | One card payment | Has the state machine in its `status` column |
| `payment_attempts` | One authorization sent to one gateway | Its id is our reference at the gateway |
| `refunds` | One refund of a payment | `pending` refunds reserve their amount |

### The Redis keys

| Key | Holds | Written by |
|---|---|---|
| `ratelimit:<account or merchant>` | Token bucket: tokens left and last refill time | `app/ratelimit.py` |
| `routing:window:<gateway>` | The last 100 authorization outcomes, 1 or 0 | `app/routing.py` |
| `circuit:<gateway>` | Error streak and `open_until` | `app/routing.py` |
| `circuit:<gateway>:probe` | Set while one half-open probe is in flight | `app/routing.py` |
| `routing:decisions` | Count of routing decisions, the clock for forgetting | `app/contextual.py` |
| `routing:linucb:<gateway>` | The LinUCB matrix `A`, vector `b` and last update time | `app/contextual.py` |

### Reading order

If you only read a few files, read these, in this order:

1. `app/schema.sql`. The data model, with comments on why each column exists.
2. `app/ledger.py`. About 170 lines. Everything about locking and idempotency starts here.
3. `tests/test_concurrency.py`. Three short tests that prove the ledger works under load.
4. `app/idempotency.py`. The same claim pattern, for merchant requests.
5. `app/payments.py`. The biggest file. Read `create_payment`, `capture_payment` and `finish_capture` first.
6. `app/connectors/base.py`. How a network failure becomes one of four outcomes.
7. `app/recovery.py`. What happens to payments stuck in flight.
8. `app/routing.py`, then `app/contextual.py`.

## 4. The ledger

Code: `app/ledger.py`, `app/reconcile.py`, `app/ratelimit.py`, `app/schema.sql`.
Tests: `tests/test_transfers.py`, `tests/test_concurrency.py`, `tests/test_reconcile.py`, `tests/test_ratelimit.py`.

### 4.1 Money is an integer

Every amount is a `bigint` number of minor units. 500 rupees is `50000` paise. $12.34 is `1234` cents.

Floats cannot store most decimal fractions exactly. In Python, `0.1 + 0.2 == 0.30000000000000004`. Add up enough payments and the books stop balancing by a fraction of a paisa. Integers never have that problem.

The API is strict about it. `AmountMinor` in `app/models.py` uses `Field(strict=True)`, so `"100"` and `100.0` are rejected rather than converted. The upper limit of `10**12` keeps sums far from the `bigint` limit.

### 4.2 Double-entry bookkeeping

Money never appears or disappears. It only moves from one account to another, and each move writes two entries that sum to zero.

Here is a top-up of 1,000 to Alice's account:

| Entry | Account | `amount_minor` |
|---|---|---|
| 1 | `system:funding` (INR) | -1000 |
| 2 | Alice (INR) | +1000 |

And Alice paying Bob 300:

| Entry | Account | `amount_minor` |
|---|---|---|
| 3 | Alice | -300 |
| 4 | Bob | +300 |

Now Alice has 700, Bob has 300, and funding has -1000. The three balances add up to zero. That holds for every currency, all the time, and it is one of the checks reconciliation runs.

System accounts (`system:funding`, `system:gateway_clearing`) have `allow_negative = true`. Every other account has a `CHECK` constraint that stops its balance going below zero. The code checks funds before the constraint would fire, so the constraint is a last line of defence, not the main check.

`ledger_entries` is append-only. A trigger (`reject_ledger_change` in `schema.sql`) raises an error on any `UPDATE` or `DELETE`. To fix a mistake, you post a new transfer that reverses it. The history stays intact.

`accounts.balance_minor` is a cache. You could compute every balance by summing its entries. Keeping a cached total makes reads cheap, and reconciliation proves the cache is right.

### 4.3 Idempotency: claim, don't check

The obvious way to stop duplicates has a bug:

```
request 1: SELECT ... WHERE idempotency_key = 'k'   -> no row
request 2: SELECT ... WHERE idempotency_key = 'k'   -> no row
request 1: INSERT transfer, move money
request 2: INSERT transfer, move money             <- charged twice
```

Both requests check before either one writes. PaySentry skips the check and writes first. `post_in_transaction` starts with:

```sql
INSERT INTO transfers (idempotency_key, ...) VALUES ($1, ...)
ON CONFLICT (idempotency_key) DO NOTHING
RETURNING *
```

`idempotency_key` has a unique index. When two transactions insert the same key, Postgres makes the second one wait until the first commits or rolls back:

- If the first commits, the second insert does nothing and returns no row. The code then reads the stored response and replays it.
- If the first rolls back, the second insert goes ahead as if it were first.

So exactly one request ever runs the money-moving code for a key. `tests/test_concurrency.py::test_concurrent_duplicates_charge_exactly_once` sends 50 copies at once and checks for 1 transfer and 49 replays.

Three more details:

- **The replay is exact.** The first response is stored in a `json` column, not `jsonb`. `jsonb` reorders keys, and a replay should match the original byte for byte.
- **A key reused with a different body is an error.** The server stores a SHA-256 of the request (`request_hash`). Same key, different hash: `422 idempotency_key_reused`. Without this, a client bug that reuses keys would silently get the wrong payment's response back.
- **Errors and outcomes are different things.** An unknown account is a bad request. The code raises, the transaction rolls back, and the key is free again for a corrected request. Insufficient funds is a real outcome. It commits as `FAILED` with a 422, and every retry gets that same 422.

### 4.4 Locks

Idempotency stops one request running twice. It does nothing about two different requests spending the same money.

Alice has 100. Fifty requests each try to send 30. Without a lock, many of them read "balance 100", all decide 30 is affordable, and all subtract. Alice ends far below zero, or the `CHECK` constraint throws errors at random.

So the ledger locks both account rows before reading the balance:

```sql
SELECT id, currency, balance_minor, allow_negative
FROM accounts
WHERE id = ANY($1::uuid[])
ORDER BY id
FOR NO KEY UPDATE
```

Three choices in that statement are worth learning.

**Lock before reading.** The lock makes the other 49 transactions wait in line. Each one sees the balance left by the one before it. Exactly 3 of the 50 succeed (`test_concurrent_spends_never_overdraw`).

**Lock in id order.** Say transfer 1 is A->B and transfer 2 is B->A. If each locked its payer first, transfer 1 could hold A while transfer 2 holds B. Each then waits for the other's row forever. That is a deadlock, and Postgres kills one of them. With `ORDER BY id`, both transfers lock the lower id first. Whichever gets it first goes ahead, and the other waits on that same row. Nobody holds a row the other needs. `test_opposite_transfers_do_not_deadlock` runs 100 opposite transfers at once.

**Lock with `FOR NO KEY UPDATE`, not `FOR UPDATE`.** This one is subtle. The transfer row was inserted before the lock, and it has foreign keys to both accounts. To check a foreign key, Postgres takes a `FOR KEY SHARE` lock on each referenced row. Here is how the row lock modes conflict:

| Held \ Requested | `KEY SHARE` | `SHARE` | `NO KEY UPDATE` | `UPDATE` |
|---|---|---|---|---|
| `KEY SHARE` | ok | ok | ok | conflict |
| `SHARE` | ok | ok | conflict | conflict |
| `NO KEY UPDATE` | ok | conflict | conflict | conflict |
| `UPDATE` | conflict | conflict | conflict | conflict |

Now replay the A->B and B->A case with `FOR UPDATE`:

1. Transfer 1 inserts its row and takes `KEY SHARE` on A and B.
2. Transfer 2 inserts its row and also takes `KEY SHARE` on A and B. That is allowed, because `KEY SHARE` does not conflict with itself.
3. Transfer 1 asks for `FOR UPDATE` on A. It has to wait for transfer 2's `KEY SHARE` on A.
4. Transfer 2 asks for `FOR UPDATE` on A. It has to wait for transfer 1's `KEY SHARE` on A.

That is a deadlock, even with id ordering. `FOR NO KEY UPDATE` does not conflict with `KEY SHARE`, so step 3 succeeds and step 4 waits normally. It is also the right mode by meaning: we change the balance, never the primary key. Exercise 1 in [section 10](#10-exercises-break-it-on-purpose) lets you watch this deadlock happen.

### 4.5 Reconciliation

`app/reconcile.py` answers one question: do the cached numbers agree with the ledger? It checks five things:

1. Each account balance equals the sum of its entries.
2. Each successful transfer has exactly two entries that sum to zero. A failed transfer has none.
3. Each currency sums to zero across all accounts.
4. Each payment's `amount_captured` equals its capture posting.
5. Each payment's `amount_refunded` equals its succeeded refunds and their postings.

It runs all five inside one `REPEATABLE READ` read-only transaction. That gives it one snapshot of the database. Without it, a transfer that commits between check 1 and check 3 could look like a mismatch that is not real.

Every test client runs reconciliation after its test ends (see `make_client` in `tests/conftest.py`). So every test in the suite is also a ledger consistency test.

### 4.6 Rate limiting

`app/ratelimit.py` gives each paying account, or each merchant for payments, a token bucket in Redis. With the defaults (capacity 10, refill 5 per second):

- A burst of 10 requests all pass and empty the bucket.
- The 11th is refused with 429. It needs 1 token at 5 per second, so it should wait 200 ms. `Retry-After` only takes whole seconds, so the header says 1.
- After 2 seconds of quiet, the bucket is full again.

The bucket logic is a Lua script. Redis runs a script start to finish with nothing else in between. So two app servers can never both read "1 token left" and both spend it. The script reads time with `redis.call('TIME')`, so app servers with different clocks still agree.

The limiter fails open. If Redis is down, the request goes through and a warning is logged. Refusing every payment because the rate limiter is down is a worse failure than briefly having no rate limit. `RATE_LIMIT_FAIL_OPEN=false` flips that.

### Check yourself

<details>
<summary>Why not catch the unique violation instead of using <code>ON CONFLICT DO NOTHING</code>?</summary>

In Postgres, an error inside a transaction aborts the whole transaction. You would need a savepoint around the insert to recover and read the stored response. `ON CONFLICT DO NOTHING` gets the same result with no error and no savepoint.
</details>

<details>
<summary>A transfer fails with insufficient funds. Can the client retry with the same key after topping up?</summary>

No. Insufficient funds is a committed outcome, so every retry with that key replays the same 422. The client needs a new key for a new attempt. That is deliberate: a key names one attempt, and that attempt failed.
</details>

<details>
<summary>Why does <code>accounts</code> need <code>allow_negative</code> at all?</summary>

So system accounts can go negative. `system:funding` pays for every top-up, so it goes down as balances go up. Because it can go negative, the sum of all accounts in a currency stays exactly zero.
</details>

<details>
<summary>Reconciliation reports a <code>balance_mismatch</code>. What does that tell you?</summary>

Some code changed `balance_minor` without writing matching ledger entries, or the reverse. Since all postings go through `post_in_transaction`, which does both in one transaction, it points to code that went around the ledger.
</details>

## 5. Payments

Code: `app/payments.py`, `app/idempotency.py`, `app/cards.py`, `app/auth.py`, `app/routes/payments.py`.
Tests: `tests/test_payments.py`, `tests/test_payment_lifecycle.py`, `tests/test_payment_concurrency.py`, `tests/test_payment_model.py`, `tests/test_cards.py`.

### 5.1 How card payments work

A card payment usually happens in two steps:

1. **Authorize.** The card issuer checks the card and puts a hold on the money. Nothing has moved yet.
2. **Capture.** The merchant claims the held money, usually when the order ships. They can capture less than the hold (a partial capture).

Instead of capturing, the merchant can **void** the authorization to release the hold. After a capture, they can **refund** some or all of it, in one go or in several pieces.

PaySentry supports both `capture_method: "automatic"` (authorize and capture in one call) and `"manual"` (authorize now, capture later).

### 5.2 Merchants and authentication

`POST /merchants` (admin only) creates a merchant, plus one account per currency owned by `merchant:<id>`. It returns a secret key like `sk_...` exactly once.

The database stores only the key's SHA-256. If someone steals the database, they still cannot call the API as a merchant. Passwords need slow hashes like bcrypt because people pick guessable ones. This key is 32 random bytes, so nobody can guess it, and a fast hash is fine.

A payment that belongs to another merchant answers `404`, not `403`. A `403` would confirm the payment id exists.

The admin key is compared with `hmac.compare_digest`. A plain `==` stops at the first wrong character, so its timing leaks how much of a guess was right.

### 5.3 Card data

The full card number must never be stored or logged. `app/cards.py` enforces that in several places:

- `number` and `cvc` are pydantic `SecretStr`. Printing a card shows `**********`.
- Only `brand` and `last4` reach the `payments` table.
- The idempotency fingerprint uses `card.fingerprint()`: brand, last 4 digits and expiry. Hashing the full number would not be safe either. The first 6 digits identify the bank, and the last 4 are stored anyway, so only about a million candidates are left. A laptop can hash all of them in under a second.
- FastAPI's default validation error echoes back the bad input, which could be a whole card. `app/errors.py` replaces that handler with one that keeps only the error type, location and message.

The number is also checked with the Luhn algorithm (`luhn_valid`), which catches most typos, and expired cards are rejected.

### 5.4 The state machine

```
processing --> authorized --> capture_pending --> captured --> refunded
     |              |                                ^
     |              +--> void_pending --> voided     |
     +--> captured (automatic capture) --------------+
     +--> failed
```

The `*_pending` and `processing` states mean "we have asked the gateway and are waiting". The full table of transitions is in [payments.md](design/payments.md#payment-state-machine).

Every transition is a single conditional `UPDATE`:

```sql
UPDATE payments SET status = 'capture_pending', ...
WHERE id = $1 AND merchant_id = $2 AND status = 'authorized'
RETURNING *
```

If no row comes back, the payment was not in the right state, and the API answers `409 invalid_state`.

This one statement also handles races. Twenty captures arrive at once. All twenty run this `UPDATE`. The first one locks the row and changes it. The other nineteen wait for that lock. Under Postgres's default `READ COMMITTED` isolation, once the first commits, each waiting `UPDATE` re-checks its `WHERE` clause against the new row. The status is now `capture_pending`, not `authorized`, so nothing matches. One capture reaches the gateway and nineteen get 409 (`test_concurrent_captures_succeed_exactly_once`).

### 5.5 The three-step pattern

A transfer runs in one transaction because it never leaves the database. A payment has to call a gateway over the network, which can take seconds. Holding a transaction open that long would keep the payment's row locked, and every other request on that payment would stall behind it.

So every gateway operation runs in three steps:

1. **Transaction A.** Claim the idempotency key. Move the payment to an in-flight state. Commit.
2. **The gateway call**, with no transaction open.
3. **Transaction B.** Record the outcome, post ledger entries, store the response under the key. Commit.

Step 1 commits a record that says "we asked the gateway". If the process dies before step 3, that record is still there, and the recovery job (section 6.4) uses it to finish the job.

Step 3 lives in the `finish_*` functions: `finish_authorization`, `finish_capture`, `finish_void` and `finish_refund`. Each one only acts if the row is still in its in-flight state. The request path and the recovery job both call them, and whichever gets there first wins. The other one finds nothing to do and returns `None`.

### 5.6 Refunds and reservations

A payment can have many refunds, and each refund also goes through the three steps. Here is the danger: 20 refunds of 100 arrive for a payment with 1,000 captured. If each one only checked `amount_captured - amount_refunded`, all 20 would see 1,000 available. They would all pass the check while waiting on the gateway, and 2,000 would be refunded.

So transaction A locks the payment row and counts the refunds that are still pending:

```
available = amount_captured - amount_refunded - sum(pending refunds)
```

Inserting a `pending` refund reserves its amount. The next refund waits for the payment row lock, then sees that reservation. When the gateway answers, transaction B marks the refund `succeeded` (and adds it to `amount_refunded`) or `failed` (which releases the reservation). Exactly 10 of the 20 succeed (`test_concurrent_refunds_never_exceed_the_capture`).

### 5.7 Where the money goes

Each currency has a `system:gateway_clearing` account. It stands for money the gateways owe us. It goes negative as payments are captured.

| Event | From | To |
|---|---|---|
| Capture | gateway clearing | merchant |
| Refund | merchant | gateway clearing |

An authorization posts nothing. The hold sits at the card issuer, so no money has moved yet.

Postings go through `ledger.post_in_transaction` inside transaction B. The payment's new state and its money movement commit together or not at all. Each posting uses a fixed internal key, `payment-capture:<payment id>` or `payment-refund:<refund id>`. Even if a bug tried to post a capture twice, the ledger's own idempotency would turn the second into a replay.

### 5.8 Merchant idempotency

Merchant requests use the `idempotency_keys` table (`app/idempotency.py`), scoped per merchant. Two merchants can both use the key `order-1` without clashing. The claim works the same way as in the ledger: `INSERT ... ON CONFLICT DO NOTHING`.

| Situation | Answer |
|---|---|
| New key | Run the request |
| Key completed, same request | The stored response, with `Idempotent-Replayed: true` |
| Key still in progress | `409 request_in_progress` |
| Same key, different request | `422 idempotency_key_reused` |

What gets stored depends on what happened:

- A definite outcome (approved, declined) is stored and replayed forever.
- A request error (unknown payment, wrong state) rolls back transaction A, so the key stays free.
- An unknown gateway outcome answers `202` and stores nothing. The key stays `in_progress` until recovery stores the real answer.
- A capture, void, or refund that certainly was not processed returns `503` and releases its key for retry.
- A create request that exhausts its gateway attempts stores a failed payment and replays that result.
- A create request with no eligible gateway returns `503 no_gateway_available` and leaves its key free.

### 5.9 Lock order

Deadlocks happen when two transactions take the same locks in different orders. Every transaction in PaySentry takes locks in this order:

1. The payment row.
2. Refund rows.
3. Account rows, in id order, through the ledger.

No code path locks a payment after an account. You can see this in `finish_refund`, which locks the payment before the refund even though it starts from the refund id.

### 5.10 One payment, start to finish

This is a manual-capture payment of 500 rupees, captured at 400, then refunded 150.

| Step | Request | What changes |
|---|---|---|
| 1 | `POST /payments` | Tx A: `idempotency_keys` row `in_progress`. `payments` row `processing`. `payment_attempts` row `pending` |
| 2 | (gateway call) | The router picks a gateway. The connector sends the attempt id as our reference. The gateway approves |
| 3 | | Tx B: payment `authorized`, attempt `succeeded`, key `completed` with a 201 body. No ledger entries |
| 4 | `POST /payments/{id}/capture` `{40000}` | Tx A: new key `in_progress`. Payment `capture_pending`, `pending_capture_minor = 40000` |
| 5 | (gateway call) | Reference `capture:<payment id>`. Approved |
| 6 | | Tx B: payment `captured`, `amount_captured = 40000`. Ledger: clearing -40000, merchant +40000. Key `completed` with a 200 body |
| 7 | `POST /payments/{id}/refunds` `{15000}` | Tx A: payment row locked, available = 40000. `refunds` row `pending` |
| 8 | (gateway call) | Reference is the refund id. Approved |
| 9 | | Tx B: refund `succeeded`, `amount_refunded = 15000`. Ledger: merchant -15000, clearing +15000 |

The merchant's balance is now 25,000 paise, and reconciliation is clean.

### Check yourself

<details>
<summary>Why is a declined payment a <code>201</code> and not an error?</summary>

The request worked. A payment record was created, and the gateway gave a definite answer. The decline is the outcome, shown in `status` and `failure_reason`. It is stored under the key and replayed, just like an approval.
</details>

<details>
<summary>What stops a capture and a void racing on the same payment?</summary>

Both are conditional updates that require `status = 'authorized'`. Whichever locks the row first moves it to `capture_pending` or `void_pending`. The other re-checks its `WHERE`, matches nothing, and gets 409. `test_capture_and_void_racing_have_one_winner` checks this.
</details>

<details>
<summary>Why not keep one long transaction around the gateway call and skip recovery entirely?</summary>

Two reasons. A slow gateway would hold row locks and pile up every other request on that payment, and it would tie up a pooled connection the whole time. And it does not even solve the problem: if the process dies mid-call, the transaction rolls back and erases the only record that the gateway was asked. The gateway may have charged the card anyway.
</details>

<details>
<summary>Why does the refund check subtract pending refunds?</summary>

Pending refunds have been sent, or are about to be sent, to the gateway. Their money may already be leaving. Without subtracting them, refunds that overlap in time could add up to more than was captured.
</details>

## 6. Gateways and unknown outcomes

Code: `app/connectors/`, `app/recovery.py`, `gateway/app.py`.
Tests: `tests/test_connectors.py`, `tests/test_gateway_mock.py`, `tests/test_payment_gateways.py`, `tests/test_recovery.py`, `tests/transports.py`.

### 6.1 The four outcomes

This is the most important idea in the project. When you call a gateway, you get one of four results:

| Outcome | Meaning | What is safe next |
|---|---|---|
| `approved` | It did it | Record success |
| `declined` | It refused, for good | Record failure |
| `not_processed` | It certainly did nothing | Give up, or try another gateway |
| `unknown` | It may or may not have acted | Only one thing: find out |

`unknown` is the dangerous one. Say you sent a charge and the reply never came. Maybe the request never arrived. Maybe the gateway charged the card and the reply got lost. If you treat it as failed and the customer tries again, they may pay twice. If you try another gateway, they may pay twice.

`HttpConnector.send` in `app/connectors/base.py` sorts every result into one of these:

| What happened | Outcome | Why |
|---|---|---|
| Connection refused, or the connect timed out | `not_processed` | The request never left our machine |
| Our 2-second deadline passed after sending | `unknown` | The gateway may be working on it |
| Connection dropped mid-response | `unknown` | Same |
| HTTP `503` | `not_processed` | The gateways promise 503 means nothing was done |
| HTTP `500`, `502`, `504` | `unknown` | A server error tells you nothing about whether it acted |
| HTTP `404` on a lookup | `not_found` | The gateway has no charge with our reference |
| Any other HTTP `4xx` | `declined` | A deliberate refusal |
| HTTP `200` | `approved` or `declined` | Read the body |

One small trick: the httpx connect timeout is half the overall deadline (`app/main.py`). A connection that never opens is caught as a connect error, which is `not_processed`. Otherwise the overall deadline would fire first and wrongly call it `unknown`.

### 6.2 Our reference makes retries safe

Every request to a gateway carries our own reference:

- An authorization sends the `payment_attempts` row id.
- A capture sends `capture:<payment id>`, and a void sends `void:<payment id>`.
- A refund sends the refund id.

The mock gateways, like real ones, are idempotent on that reference. If the same reference arrives twice, they return the first result and do not act again. That makes two recovery moves safe:

- **Look up** an authorization by reference: "did you charge attempt X?"
- **Re-send** a capture, void or refund with the same reference. If the gateway already did it, it just returns the earlier result.

Authorizations are looked up but never re-sent. Re-sending one would need the card number, and PaySentry does not keep it.

### 6.3 Connectors

The two mock gateways have deliberately different APIs, the way real gateways do:

| | Alpha | Beta |
|---|---|---|
| Paths | `/v1/charges` | `/api/payments` |
| Field names | snake case | camel case |
| Money | Integer minor units | Decimal string in major units, like `"123.45"` |
| Card | Nested object | Flat fields, expiry as `MM/YY` |
| A refusal | HTTP 402 | HTTP 200 with `"result": "REFUSED"` |

A connector hides all of that behind one interface, `Connector` in `app/connectors/base.py`, with five methods: `authorize`, `lookup`, `capture`, `void` and `refund`. Each returns a `GatewayResult`. The rest of the code never sees a gateway's own format.

Beta's money conversion uses `Decimal`, never `float`. `to_major(5)` is `"0.05"`. `from_major` refuses `"1.005"`, because that is a fraction of a paisa.

### 6.4 Recovery

`app/recovery.py` runs every `RECOVERY_INTERVAL_SEC` (10) inside the API process. Admins can also trigger a pass with `POST /internal/recovery/run`. Each pass looks for work that has been in flight longer than `RECOVERY_AFTER_SEC` (60):

| Stuck in | Recovery does | Result |
|---|---|---|
| Attempt `pending` or `unknown`, payment `processing` | Look up the charge by reference | Found: finish it. Not found: payment `failed` with `gateway_no_record` |
| Payment `capture_pending` | Re-send the capture | `captured`, or back to `authorized` |
| Payment `void_pending` | Re-send the void | `voided`, or back to `authorized` |
| Refund `pending` | Re-send the refund | `succeeded` or `failed` |

If the gateway still does not answer clearly, the row is left for the next pass.

Why wait 60 seconds? "Not found" only means something once the original request can no longer arrive. Picture a request stuck in a slow network. Recovery looks it up, gets "not found", and marks the payment failed. Then the original request arrives and the gateway charges the card. With a 2-second deadline, a request still travelling after a minute is not a realistic case.

`pending` also covers crashes. If the process dies after transaction A and before transaction B, the attempt stays `pending` forever. Recovery treats it like `unknown`.

Several app instances may run recovery at the same time. Each pass claims rows like this:

```sql
UPDATE payments SET lease_until = now() + interval '30 seconds'
WHERE id IN (
    SELECT id FROM payments
    WHERE status = $2 AND ... AND (lease_until IS NULL OR lease_until < now())
    LIMIT 100
    FOR NO KEY UPDATE SKIP LOCKED
)
```

`SKIP LOCKED` makes a worker skip rows another worker is claiming right now, instead of waiting for them. The lease then keeps other workers away for 30 seconds after the claim commits, while this worker calls the gateway. If the worker crashes, the lease runs out and another worker picks the row up. And because every `finish_*` is guarded by status, two workers that somehow both act on one row still finish it only once.

### 6.5 A timeout, start to finish

A merchant captures a payment, and the gateway charges it but hangs before replying.

1. Transaction A: payment `capture_pending`, key `in_progress`.
2. The capture call hits the 2-second deadline. Outcome: `unknown`.
3. The API answers `202` with the payment body. Nothing is stored under the key.
4. The merchant retries with the same key and gets `409 request_in_progress`. They can poll `GET /payments/{id}`.
5. A minute later, recovery claims the payment and re-sends the capture with reference `capture:<payment id>`.
6. The gateway sees that reference, has already captured it, and returns the original result.
7. Recovery calls `finish_capture`. The payment becomes `captured`, the ledger posts, and the 200 response is stored under the merchant's key.
8. The merchant retries again and gets the 200. It is the same body they would have got with no timeout.

`test_unknown_capture_is_recovered` runs this exact story.

### 6.6 The mock gateway

`gateway/app.py` is a small FastAPI app that keeps charges and refunds in memory. Its behaviour is set with `POST /_control` (or directly in tests):

| Setting | Effect |
|---|---|
| `latency_ms` | Wait before answering |
| `decline_rate` | Share of authorizations declined |
| `unavailable_rate` | Share of requests answered `503` without acting |
| `unknown_rate` | Share of requests that act, then hang for `hang_ms` |
| `decline_by_brand`, `large_amount_*` | Decline rates per card brand and for large amounts, for routing experiments |
| `seed` | Makes the random draws repeat |

Some test cards also behave in fixed ways on every gateway. `4000000000000002` is always declined, and `4000000000000341` approves but rejects captures. The full list is in [payments.md](design/payments.md#test-cards).

Process crashes are simulated in tests by `tests/transports.py`. `CrashOnce` wraps a gateway's transport and kills the first request, either before or after the gateway acts.

### Check yourself

<details>
<summary>Why is HTTP 500 <code>unknown</code> but 503 <code>not_processed</code>?</summary>

A 500 means something broke on the gateway's side, possibly after it charged the card. A 503 is a promise, by agreement with these gateways, that the request was turned away before doing anything. Real gateways document which of their errors are safe to retry. Classifying them correctly is part of writing a connector.
</details>

<details>
<summary>Recovery finds an authorization the gateway has no record of. Why is it safe to fail the payment?</summary>

Because recovery only looks at attempts sent more than 60 seconds ago, and the deadline is 2 seconds. A request that has not arrived after a minute will not arrive. If recovery checked straight away, a request still in transit could land after the payment was marked failed.
</details>

<details>
<summary>Why don't the request path and recovery need a lock between them?</summary>

Both finish through the same `finish_*` functions, and each starts with an update or select guarded by the in-flight status. The first to commit moves the row out of that status. The second finds no matching row and returns `None`. Then the request path reads whatever recovery stored (`stored_response`).
</details>

## 7. Routing

Code: `app/routing.py`, `app/contextual.py`, `scripts/simulate_routing.py`.
Tests: `tests/test_routing.py`, `tests/test_contextual.py`.

With several gateways, PaySentry has to choose one for each payment. Gateways differ in which cards they approve, and they go down. `Router.plan` in `app/routing.py` builds an ordered list of gateways to try in three steps.

Routing only applies to authorizations. A capture, void or refund must go to the gateway that holds the authorization.

### 7.1 Rules

Each gateway can list the currencies and card brands it takes in `GATEWAYS`. A gateway is eligible when it accepts both the payment's currency and its brand. If none is eligible, or every eligible one has an open circuit, the API answers `503 no_gateway_available` and releases the key.

### 7.2 Circuit breakers

A gateway that is down still costs every payment a wasted call and a delay. A circuit breaker stops calling it for a while.

| State | Meaning | What happens next |
|---|---|---|
| Closed | Normal | Opens after 5 errors in a row |
| Open | Skipped | After 30 seconds it becomes half-open |
| Half-open | One trial request is allowed | Success closes it. Failure opens it again |

Only `not_processed` and `unknown` count as errors. A decline proves the gateway is up and answering, so it resets the error streak.

The state lives in Redis so every API instance shares it. The "only one trial request" rule is a Redis `SET NX` with an expiry. `NX` means "set only if missing", so exactly one caller across all instances gets to be the probe.

### 7.3 Success-rate ranking (Thompson sampling)

This is the default strategy. Each gateway keeps its last 100 authorization outcomes in Redis, as 1 (approved) or 0 (anything else).

The naive approach is to always pick the gateway with the best rate. The trouble is that a gateway that had a bad hour never gets traffic again, so you never find out it recovered.

Thompson sampling fixes that with a random draw. For each payment, each gateway gets one random number from a Beta distribution shaped by its record:

- Gateway A approved 90 of its last 100. It draws from `Beta(91, 11)`, which sits around 0.89.
- Gateway B approved 40 of its last 100. It draws from `Beta(41, 61)`, which sits around 0.40.

The gateway with the highest draw goes first. A almost always wins. When B's record is thinner or closer to A's, its draws spread wider, so it wins sometimes. Those occasional payments refresh B's window. If B has recovered, its draws climb and it earns traffic back on its own. A new gateway starts at `Beta(1, 1)`, which is uniform between 0 and 1, so it gets tried straight away.

Declines count against a gateway on purpose. Gateways really do approve different shares of the same cards, and that difference is what this strategy exists to find.

### 7.4 Failover

The plan is a list, so if the first gateway fails, the next one can be tried, up to 3 in total. This is where section 6.1 pays off:

| Outcome | Try the next gateway? |
|---|---|
| `approved` | No, done |
| `declined` | No. The card was refused. Retrying elsewhere is a product decision this project does not make |
| `unknown` | Never. The first gateway may have charged |
| `not_processed` | Yes. Nothing happened, so it is safe |

Each try gets its own `payment_attempts` row and its own reference. On failover, `_fail_over` marks the old attempt `failed` and points the payment at the new gateway, in one transaction.

### 7.5 Contextual ranking (LinUCB)

Success-rate ranking gives each gateway one number. But a gateway might approve 95% of Visa and 55% of Mastercard. One average hides that. `ROUTING_STRATEGY=contextual` learns a separate answer for each kind of payment.

**Features.** Each payment becomes a vector `x` of 15 zeros and ones (`features` in `app/contextual.py`). A Visa payment of 6,000 rupees (600,000 paise) becomes:

```
bias  visa mc amex other  <500 500-5k 5k-50k >50k  INR USD  visa&large mc&large amex&large other&large
 1     1   0   0    0      0     0      1     0     1   0       1          0         0          0
```

The last four are "cross" features. A linear model adds up the effects of features. On its own it cannot say "fine for Visa, except large Visa payments". A cross feature gives it a separate weight for exactly that case.

**The model.** For each gateway, LinUCB keeps the running sums of a ridge regression from `x` to approval:

```
A = lambda * I + sum of (x times x transposed)     a 15 x 15 matrix
b = sum of (reward * x)                           a 15-vector
theta = A^-1 b                                    the learned weight of each feature
score = theta . x  +  alpha * sqrt(x . A^-1 x)
```

The score has two parts:

- `theta . x` is the predicted approval for this payment.
- `alpha * sqrt(x . A^-1 x)` is an exploration bonus. It is large when the gateway has seen few payments like this one, and small when it has seen many.

The router tries gateways from the highest score down. So a gateway gets tried in segments where little is known about it, and it stops getting payments in segments where it is known to be bad.

**Centred rewards.** An approval counts +0.5 and anything else -0.5, not 1 and 0. Ridge regression pulls a gateway with no data toward a prediction of 0. With 0/1 rewards, 0 would mean "approves nothing", the same score as a gateway that has declined everything. With centred rewards, 0 means "50%, no idea". A mutation check caught this: before centring, the idle-gateway test passed for the wrong reason.

**Forgetting.** Gateways change, so old evidence has to fade. Each routing decision bumps a counter in Redis. Before a gateway's sums are read or updated, they are multiplied by `0.998 ^ (decisions since its last update)`. This does two things:

- A gateway that gets worse loses its good name within a few hundred payments.
- A gateway that gets no traffic slowly loses its bad name too. Its sums shrink, its exploration bonus grows, and eventually it is tried again. That is how a recovered gateway gets noticed without anyone resetting it.

Decay is counted in decisions, not seconds. A quiet night forgets nothing, and the simulation behaves the same at any speed.

**Shared state.** Each gateway's `A`, `b` and last update counter are one JSON value in Redis. The `OBSERVE` Lua script decays and updates them in one atomic step, so two API instances can never overwrite each other's update. Scoring reads every gateway in one round trip, and NumPy solves the 15 x 15 system. `scores` solves `A z = x` once and gets both terms from `z`, because `A` is symmetric.

### 7.6 The simulation

`scripts/simulate_routing.py` sends thousands of payments through the real app, with real Postgres and Redis, to three in-process mock gateways. It runs two scenarios:

- **Outages.** Alpha starts healthy, then declines 60%, then goes fully down, then recovers.
- **Segments.** Mixed Visa, Mastercard and Amex traffic. Alpha is good at Visa, beta is good at Mastercard, and gamma is average at everything.

```sh
docker compose up -d db redis
uv run python scripts/simulate_routing.py 1000
```

The full results are in [routing.md](design/routing.md#results). In short: learned routing matters when a gateway degrades, the circuit breaker matters when one goes down, and contextual routing matters when gateways differ by segment.

### Check yourself

<details>
<summary>Why does a decline reset the circuit breaker's error streak?</summary>

The breaker answers "is this gateway reachable and working?" A decline is a real answer from a working gateway. Treating declines as errors would open the circuit on a gateway that is fine but sees a run of bad cards.
</details>

<details>
<summary>What does the router do if Redis is down?</summary>

It cannot read health data, so it returns the eligible gateways in priority order (the order in `GATEWAYS`). `acquire` returns `True`, so no circuit blocks a call. Payments keep flowing without smart routing. `test_redis_down_falls_back_to_priority_with_circuits_closed` covers this.
</details>

<details>
<summary>Why does contextual routing react more slowly than success rate to a sudden drop?</summary>

Its memory is longer. A decay of 0.998 per decision keeps an effective memory of about 1 / (1 - 0.998) = 500 decisions. The success window holds only the last 100 outcomes. More old good history has to fade before the drop shows.
</details>

<details>
<summary>Why is <code>alpha</code> (the exploration weight) 0.2 and not higher?</summary>

It was tuned on the segment simulation. With 3 gateways and dozens of segments, a higher value spent too many payments exploring. These values fit the simulated gateways. Real traffic would need its own tuning. See "How the contextual defaults were chosen" in [routing.md](design/routing.md#how-the-contextual-defaults-were-chosen).
</details>

## 8. How it is tested

Tests: everything in `tests/`. Fixtures: `tests/conftest.py`. CI: `.github/workflows/ci.yml`.

### Real databases, no mocks

Every test runs against real Postgres and Redis. Locking, isolation and deadlocks are behaviours of the real database. A mock would happily pass a test that deadlocks in production.

`make_client` in `conftest.py` builds a fresh app for each test. It:

- drops and recreates the `public` schema in `paysentry_test`, and flushes Redis database 1
- serves the mock gateways in-process through `httpx.ASGITransport`, so there is no network
- sets the rate limit very high, turns the recovery loop off, and uses `priority` routing, so tests are predictable
- reconciles the ledger after the test and fails the test if anything is off

### Concurrency tests

These use `asyncio.gather` to fire many requests at once. The pool holds 20 connections, so up to 20 transactions really overlap. For payments, the mock gateway waits 50 ms per call, which keeps requests overlapping between transaction A and transaction B.

### Model-based testing

`tests/test_payment_model.py` holds a tiny Python model of the payment state machine: about 30 lines with `capture`, `void` and `refund` methods. For each of 30 seeds, it runs 12 random operations against both the real API and the model. It checks that every response code and every resulting state match. When something fails, the message names the seed and step, so you can replay it exactly.

This finds bugs nobody thought to write a test for, like an odd sequence of partial capture, refund, refund, void.

### Fault injection

`tests/transports.py` has httpx transports that fail on purpose:

- `FailingTransport` raises an error, optionally after the gateway has acted.
- `StatusTransport` answers every request with one status code.
- `CrashOnce` kills the first request, before or after the gateway acts, to imitate the process dying mid-call.

### Mutation checks

A test that has never failed might not test anything. During the build, I broke the code on purpose to check that the right test fails. Section 10 turns these into exercises.

## 9. Startup and configuration

`app/main.py` builds the app in `create_app` and sets everything up in `lifespan`:

1. Create the asyncpg pool and the Redis client.
2. Apply `schema.sql`. Every statement in it is safe to run again (`IF NOT EXISTS`, `ON CONFLICT DO NOTHING`). Several instances may start at once, so they take turns with a Postgres advisory lock (`apply_schema` in `app/db.py`).
3. Build one httpx client and one connector per gateway.
4. Create the rate limiter and the router.
5. Start the recovery loop, unless `RECOVERY_INTERVAL_SEC` is 0.

On shutdown, it stops the loop and closes the clients and the pool.

Settings come from environment variables, or from a `.env` file (`app/config.py`). `.env.example` lists every one with its default. The most useful ones to change:

| Variable | Default | What it does |
|---|---|---|
| `ROUTING_STRATEGY` | `success_rate` | `priority`, `success_rate` or `contextual` |
| `GATEWAYS` | alpha and beta on localhost | JSON: each gateway's API flavour, URL, currencies and brands |
| `GATEWAY_TIMEOUT_SEC` | `2` | Deadline per gateway call |
| `RECOVERY_AFTER_SEC` | `60` | How long work must be stuck before recovery touches it |
| `RATE_LIMIT_CAPACITY` | `10` | Token bucket burst size |
| `RATE_LIMIT_FAIL_OPEN` | `true` | Allow requests when Redis is down |

## 10. Exercises: break it on purpose

Each exercise changes one line and runs one test. Watching a test fail for a reason you predicted is the fastest way to understand why the code is written as it is. Undo each change with `git checkout -- <file>` afterwards.

You need the containers running: `docker compose up -d db redis`.

**1. Watch the deadlock.** In `app/ledger.py`, change `FOR NO KEY UPDATE` to `FOR UPDATE` in the account lock.

```sh
uv run pytest tests/test_concurrency.py::test_opposite_transfers_do_not_deadlock
```

Expect `DeadlockDetectedError`. Section 4.4 explains why.

**2. Double spend.** In `app/ledger.py`, delete the `FOR NO KEY UPDATE` line from the account lock entirely.

```sh
uv run pytest tests/test_concurrency.py::test_concurrent_spends_never_overdraw
```

More than 3 transfers will succeed, or the balance check constraint will fire.

**3. Double capture.** In `capture_payment` in `app/payments.py`, remove `AND status = 'authorized'` from the `UPDATE`.

```sh
uv run pytest tests/test_payment_concurrency.py tests/test_payment_model.py
```

**4. Over-refund.** In `refund_payment` in `app/payments.py`, change `available = payment["amount_captured"] - payment["amount_refunded"] - pending` so it no longer subtracts `pending`.

```sh
uv run pytest tests/test_payment_concurrency.py::test_concurrent_refunds_never_exceed_the_capture
```

**5. Unsafe failover.** In `create_payment` in `app/payments.py`, change `if result.outcome != "not_processed":` to `if result.outcome not in ("not_processed", "unknown"):`.

```sh
uv run pytest tests/test_routing.py::test_no_failover_after_an_unknown_outcome
```

The test fails because beta charges the card after alpha timed out, and alpha may have charged it too.

**6. Double finish.** In `finish_authorization` in `app/payments.py`, remove `AND status = 'processing'` from the `UPDATE`.

```sh
uv run pytest tests/test_recovery.py::test_concurrent_finishers_record_an_outcome_once
```

**7. No forgetting.** In `ContextualModel.__init__` in `app/contextual.py`, set `self._gamma = 1.0`.

```sh
uv run pytest tests/test_contextual.py::test_forgets_old_evidence_after_a_regime_change tests/test_contextual.py::test_idle_gateway_is_explored_again
```

**8. Trip a circuit by hand.** Start the full stack with priority routing, so alpha is always tried first:

```sh
ROUTING_STRATEGY=priority docker compose up --build
```

Take gateway alpha down:

```sh
curl -s localhost:9001/_control -H 'content-type: application/json' -d '{"unavailable_rate": 1.0}'
```

Send six INR Visa payments with the commands from the README, each with a new `Idempotency-Key`. They all succeed through failover. Now look at the breaker with `docker compose exec redis redis-cli HGETALL circuit:alpha`. You should see `open_until`. Set `unavailable_rate` back to `0.0`, wait 30 seconds, and send one more payment. That payment is the probe, and the hash should be gone afterwards.

**9. Tune the simulation.** In `scripts/simulate_routing.py`, add `"routing_alpha": 0.5` or `"routing_decay": 0.99` to the contextual entry in `SEGMENT_SETUPS`. Run the simulation and compare the segment results with the ones in [routing.md](design/routing.md#results).

## 11. Glossary

| Term | Meaning |
|---|---|
| Authorization | The issuer checks the card and holds the money. Nothing moves yet |
| Capture | The merchant claims held money. This is when PaySentry posts to the ledger |
| Void | Releasing an authorization without capturing it |
| Refund | Returning captured money, in whole or in part |
| Minor units | The smallest unit of a currency: paise, cents |
| Idempotency key | A client-chosen id for one attempt. Sending it again returns the first result instead of acting again |
| Double entry | Every movement of money is two entries, a debit and a credit, that sum to zero |
| Clearing account | A system account for money the gateways owe us |
| Reconciliation | Checking that cached totals agree with the ledger |
| Gateway | The outside service that talks to card networks. Here they are mocks |
| Connector | Our adapter for one gateway's API |
| Reference | The id we send with each gateway request, so the gateway can recognise a repeat |
| In-flight state | `processing`, `capture_pending`, `void_pending` or a `pending` refund: we asked and have no answer yet |
| Recovery | The background job that settles in-flight work after a timeout or crash |
| Lease | A short claim on a row (`lease_until`) so only one recovery worker handles it |
| `SKIP LOCKED` | A Postgres option that skips rows another transaction has locked, instead of waiting |
| Circuit breaker | Stops calling a failing gateway for a cooldown, then lets one probe through |
| Failover | Trying the next gateway after the first certainly did nothing |
| Multi-armed bandit | Choosing between options while still learning which is best |
| Thompson sampling | A bandit method that picks the option with the highest random draw from each option's belief |
| LinUCB | A bandit method that predicts each option's reward from features, plus a bonus for uncertainty |
| Model-based test | Random operations run against the real system and a simple model, compared step by step |
| Mutation check | Breaking the code on purpose to confirm a test catches it |

## 12. Further reading

- Brandur Leach, [Implementing Stripe-like Idempotency Keys in Postgres](https://brandur.org/idempotency-keys). The merchant idempotency design follows this article.
- PostgreSQL documentation, [Explicit Locking](https://www.postgresql.org/docs/16/explicit-locking.html). The row lock conflict table in section 4.4 comes from here.
- PostgreSQL documentation, [Transaction Isolation](https://www.postgresql.org/docs/16/transaction-iso.html). Explains how `READ COMMITTED` re-checks a `WHERE` clause after waiting on a lock (section 5.4).
- Li, Chu, Langford and Schapire, [A Contextual-Bandit Approach to Personalized News Article Recommendation](https://arxiv.org/abs/1003.0146) (2010). The LinUCB paper.
- Russo, Van Roy, Kazerouni, Osband and Wen, [A Tutorial on Thompson Sampling](https://arxiv.org/abs/1707.02038).
- Martin Fowler, [CircuitBreaker](https://martinfowler.com/bliki/CircuitBreaker.html).
