# Payments design

How PaySentry takes a card payment from a merchant, moves it through its lifecycle, and records the money. Keep this file in step with the code.

The ledger underneath (accounts, transfers, double-entry entries, row locking) is covered in the [architecture](../architecture.md).

## Overview

A merchant calls the API with its secret key. It creates a payment with card details. PaySentry asks a card gateway to authorize the card, then records the outcome.

```
merchant --> POST /payments --> payments service --> connector --HTTP--> gateway
                                     |
                                     +--> payments, refunds, idempotency_keys tables
                                     +--> ledger: gateway clearing account -> merchant account
```

Gateways, connectors, and what happens when a gateway does not answer are covered in [gateways.md](gateways.md).

## The three-step pattern

Every operation that talks to a gateway follows the same three steps:

1. **Transaction A.** Claim the idempotency key. Move the payment to an in-flight state (`processing`, `capture_pending`, `void_pending`, or a `pending` refund). Commit.
2. **Gateway call.** No database transaction is open while it runs.
3. **Transaction B.** Record the outcome, post ledger entries, store the response under the idempotency key. Commit.

Holding a transaction open across a network call would keep row locks for as long as the gateway takes to answer. A slow gateway would then stall every other request on those rows. Committing the in-flight state first also leaves a record that the call was attempted. The recovery job uses that record to settle requests whose outcome is unknown.

Sometimes step 3 never happens. If the gateway does not answer in time, or the process dies between steps 1 and 3, the payment stays in its in-flight state and the idempotency key stays `in_progress`. Retries with that key get `409 request_in_progress`. The recovery job asks the gateway what happened and finishes the operation. See [gateways.md](gateways.md#recovery).

## Payment state machine

```
processing --> authorized --> capture_pending --> captured --> refunded
     |              |                                ^
     |              +--> void_pending --> voided     |
     +--> captured (automatic capture) --------------+
     +--> failed
```

| From | To | Trigger |
|---|---|---|
| `processing` | `authorized` | Gateway approves a manual-capture payment |
| `processing` | `captured` | Gateway approves an automatic-capture payment |
| `processing` | `failed` | Gateway declines, did not process the request, or has no record of it |
| `authorized` | `capture_pending` | `POST /payments/{id}/capture`, transaction A |
| `capture_pending` | `captured` | Gateway confirms the capture |
| `capture_pending` | `authorized` | Gateway rejects or did not process the capture |
| `authorized` | `void_pending` | `POST /payments/{id}/void`, transaction A |
| `void_pending` | `voided` | Gateway confirms the void |
| `void_pending` | `authorized` | Gateway rejects or did not process the void |
| `captured` | `refunded` | A refund brings `amount_refunded` up to `amount_captured` |

Every transition is one conditional update:

```sql
UPDATE payments SET status = 'capture_pending', ...
WHERE id = $1 AND merchant_id = $2 AND status = 'authorized'
RETURNING *
```

If no row comes back, the move was illegal and the API returns `409 invalid_state`. Two concurrent captures both run this statement. The second waits for the first one's row lock, then re-checks the `WHERE` clause against the new row, sees `capture_pending`, and matches nothing. So only one capture ever reaches the gateway.

A capture may ask for less than the authorized amount. The rest of the hold is released and cannot be captured later.

## Refunds

A refund is its own row (`pending`, `succeeded` or `failed`). A payment can have many.

Transaction A locks the payment row (`FOR NO KEY UPDATE`) and computes:

```
available = amount_captured - amount_refunded - sum(pending refunds)
```

If the new refund exceeds `available`, the API returns `422 refund_exceeds_available`. Otherwise the refund is inserted as `pending`, which reserves its amount. Transaction B marks it `succeeded` (and adds to `amount_refunded`) or `failed` (which frees the reservation). So concurrent refunds can never add up to more than was captured, even while they wait on the gateway.

## Idempotency

Every `POST` that changes a payment needs an `Idempotency-Key` header. Keys live in `idempotency_keys`, scoped per merchant: two merchants can use the same key without clashing.

| Situation | Response |
|---|---|
| New key | Runs the request |
| Key completed, same request | The stored response, byte for byte, with `Idempotent-Replayed: true` |
| Key still in progress | `409 request_in_progress` |
| Key used with a different method, path or body | `422 idempotency_key_reused` |

The key is claimed with `INSERT ... ON CONFLICT DO NOTHING` in transaction A.
Request errors roll transaction A back, leaving the key free for a corrected request.
Definite gateway outcomes are stored and replayed.
A `202` response is not stored; the key stays in progress until recovery stores a final response.
Capture, void, and refund release their keys when the gateway certainly did not process them, returning `503`.
Payment creation instead stores a failed payment when all authorization attempts are exhausted.
No eligible gateway returns `503 no_gateway_available` and rolls back the create request's key claim.

The request fingerprint never includes the card number. It covers the method, path and body, but the card is first reduced to brand, last 4 digits and expiry. A plain SHA-256 of a 16-digit card number can be reversed by brute force, so storing one would amount to storing the number.

The pattern follows Brandur Leach's article "Implementing Stripe-like Idempotency Keys in Postgres".

## Ledger postings

Each currency has a `system:gateway_clearing` account that may go negative. It stands for money the gateways owe us. Each merchant has one account per currency, owned by `merchant:<id>`.

| Event | Debit | Credit |
|---|---|---|
| Capture | gateway clearing | merchant |
| Refund | merchant | gateway clearing |

An authorization posts nothing. The hold sits at the card issuer, not with us.

Postings go through `ledger.post_in_transaction()`, inside transaction B, so the payment state and the money move together or not at all. Each posting has a fixed internal key (`payment-capture:<payment id>`, `payment-refund:<refund id>`), so a capture can never post twice.

`GET /internal/reconciliation` checks that each payment's `amount_captured` matches its capture posting, and that `amount_refunded` matches both its succeeded refunds and their postings.

## Lock order

To avoid deadlocks, every transaction takes locks in this order:

1. The payment row.
2. Refund rows.
3. Account rows, in id order, through the ledger.

No code path locks a payment after an account. Plain transfers lock only accounts.

The gateway clearing account takes part in every capture and refund, for every merchant. It is the obvious hot row under load. A load test that measures it is not built yet.

## Merchants and authentication

- `POST /merchants` (admin only) creates a merchant and its accounts. It returns a secret key `sk_...` once.
- The database stores only the SHA-256 of the key, plus its first 11 characters for display. The key is 32 random bytes, so a fast hash is safe. Slow hashes like bcrypt exist for low-entropy passwords.
- Merchants send `Authorization: Bearer sk_...`.
- A payment that belongs to another merchant returns `404`, not `403`, so ids cannot be probed.
- Admin routes (`/merchants`, `/accounts`, `/transfers`, `/internal/*`) need `X-Admin-Key`, compared in constant time.

## Card data

- The card number is checked with the Luhn algorithm. Expired cards are rejected.
- Only the brand and last 4 digits are stored. The full number and the CVC never reach the database or the logs.
- `number` and `cvc` are pydantic `SecretStr`, so printing or dumping a card shows asterisks.
- FastAPI's default 422 response echoes the rejected input, which for a payment can be a whole card. `app/errors.py` replaces it with one that keeps only the error type, location and message. A test sends an expired card and checks the number is not in the response.
- A test checks every stored row for the full test card number.

### Test cards

| Number | Behaviour |
|---|---|
| `4242424242424242` | Approved |
| `4000000000000002` | Declined, `card_declined` |
| `4000000000009995` | Declined, `insufficient_funds` |
| `4000000000000341` | Approved, but captures and voids are rejected |
| `4000000000005126` | Approved, but refunds fail |

Any other valid number is approved. The mock gateways implement this table, so it holds on every gateway. Their other behaviour (latency, outages, timeouts) is set per gateway: see [gateways.md](gateways.md#mock-gateway-behaviour).

## Status codes

| Code | Meaning |
|---|---|
| 200 | Capture or void succeeded |
| 201 | Payment or refund created. Check `status`: a declined payment is still a payment record |
| 202 | The gateway's outcome is unknown. The body shows the in-flight state. Recovery settles it |
| 400 | Missing `Idempotency-Key` |
| 401 | Missing or wrong API key or admin key |
| 402 | The gateway rejected a capture or void. The payment is back in `authorized` |
| 404 | No such payment for this merchant |
| 409 | `invalid_state` or `request_in_progress` |
| 422 | Validation error, `capture_exceeds_authorized`, `refund_exceeds_available`, `idempotency_key_reused` |
| 429 | Rate limited, per merchant |
| 503 | `gateway_unavailable`: the gateway did nothing. The key is free, so retry with it |

## Tests

The suite uses real Postgres and Redis.
Application clients check reconciliation at teardown unless a test explicitly disables that check to inspect a mismatch.

| File | Covers |
|---|---|
| `tests/test_payments.py` | Creating payments, declines, card validation, idempotent replays, in-flight `409`, key scoping, merchant isolation, per-merchant rate limit, no stored card numbers |
| `tests/test_payment_lifecycle.py` | Every legal and illegal capture, void and refund transition, partial amounts, gateway rejections |
| `tests/test_payment_concurrency.py` | 20 racing captures (1 wins), 10 captures racing 10 voids (1 wins), 20 racing refunds of 100 on 1000 (10 win), 50 payments across 5 merchants sharing the clearing account |
| `tests/test_payment_model.py` | 30 seeded random sequences of 12 operations each. A small Python model predicts every response code and state |
| `tests/test_cards.py` | Luhn, brands, expiry, CVC, secrecy of the number |
| `tests/test_idempotency_store.py` | Claim, replay, in-progress and mismatch rules, scoping per merchant |

Mutation checks, run by hand during the build:

- Removing `status = 'authorized'` from the capture update fails both race tests and the model-based test.
- Ignoring pending refunds when computing `available` fails the concurrent refund test.

The concurrency tests give the mock gateway 50 ms of latency, so requests overlap between transaction A and transaction B.

## Related

- Which gateway a payment goes to, and failover: [routing.md](routing.md).
- Not built yet: webhooks to merchants through a transactional outbox.
