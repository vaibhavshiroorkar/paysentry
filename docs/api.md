# API reference

The local API base URL is `http://localhost:8000`.
Interactive documentation is available at `/docs`. The generated contract is at `/openapi.json`.
Request fields come from [app/models.py](../app/models.py); handlers live in [app/routes](../app/routes).

## Authentication and request keys

Administrator routes require `X-Admin-Key`. Local Compose defaults to `dev-admin-key`.
Merchant routes require `Authorization: Bearer <merchant_api_key>`.
The merchant creation response reveals that secret once. Store it before closing the response.

Payment creation, capture, void, refunds, top-ups, and transfers require `Idempotency-Key`.
The header must contain 1 to 255 characters.
Merchant keys are scoped to the merchant across payment operations.
Transfer and top-up keys share the ledger's global transfer namespace.

Use a stable key for retries of one operation and a new key for a distinct operation.
The request fingerprint includes the action and relevant body fields.
Changing an amount while keeping a completed key returns `422 idempotency_key_reused`.
Replays carry `Idempotent-Replayed: true`.
There is no automatic key expiry or cleanup policy yet.

The rate limiter runs before replay lookup on money-changing routes.
A replay can therefore receive `429` while the merchant or account is rate limited.

## Endpoint map

| Method | Path | Access | Request | Normal result |
| --- | --- | --- | --- | --- |
| POST | `/merchants` | Admin | Merchant name | 201 merchant and new secret key |
| GET | `/balance` | Merchant | None | 200 currency balances |
| POST | `/payments` | Merchant and request key | Payment and card | 201 final payment or 202 pending payment |
| GET | `/payments/{payment_id}` | Owning merchant | None | 200 payment with refund history |
| POST | `/payments/{payment_id}/capture` | Owning merchant and request key | Optional capture amount | 200 payment or 202 pending payment |
| POST | `/payments/{payment_id}/void` | Owning merchant and request key | No body needed | 200 payment or 202 pending payment |
| POST | `/payments/{payment_id}/refunds` | Owning merchant and request key | Refund amount | 201 refund or 202 pending refund |
| POST | `/accounts` | Admin | Owner and currency | 201 empty account |
| GET | `/accounts/{account_id}` | Admin | None | 200 account |
| GET | `/accounts/{account_id}/entries` | Admin | Optional `limit` query | 200 recent entries |
| POST | `/accounts/{account_id}/top-ups` | Admin and request key | Amount | 201 transfer |
| POST | `/transfers` | Admin and request key | Source, destination, amount, currency | 201 succeeded transfer or 422 failed transfer |
| GET | `/transfers/{transfer_id}` | Admin | None | 200 transfer |
| GET | `/health` | Public | None | 200 healthy or 503 dependency failure |
| GET | `/internal/reconciliation` | Admin | None | 200 reconciliation report |
| POST | `/internal/recovery/run` | Admin | Optional `older_than_sec` query | 200 completion counts |

With `DEMO_ENABLED=true`, the app also serves the [demo routes](#demo-routes).
There are no payment-list, merchant-list, webhook, payout, or settlement endpoints yet.
The account entry list has a limit but no continuation cursor.

## Create a merchant

```http
POST /merchants
X-Admin-Key: dev-admin-key
Content-Type: application/json

{"name":"Acme"}
```

`name` must contain 1 to 100 characters.
The response includes `id`, `name`, `api_key`, `api_key_prefix`, and `created_at`.
Merchant creation also creates zero-balance INR and USD accounts.
This route has no idempotency key. Repeating it creates another merchant.

`GET /balance` returns:

```json
{"balances":[{"currency":"INR","balance_minor":0},{"currency":"USD","balance_minor":0}]}
```

## Create and read a payment

```http
POST /payments
Authorization: Bearer <merchant_api_key>
Idempotency-Key: order-1
Content-Type: application/json

{
  "amount_minor": 50000,
  "currency": "INR",
  "capture_method": "manual",
  "card": {
    "number": "4242424242424242",
    "exp_month": 12,
    "exp_year": 2030,
    "cvc": "123"
  }
}
```

All four top-level fields are required.
`capture_method` is `manual` or `automatic`.
Money must be an integer from 1 through `1000000000000`, in INR or USD minor units.
The card must pass number, expiry, and CVC validation.
Use only synthetic test cards with the mock gateways.

A successful manual authorization returns a payment shaped like this:

```json
{
  "id": "11111111-1111-4111-8111-111111111111",
  "amount_minor": 50000,
  "currency": "INR",
  "capture_method": "manual",
  "status": "authorized",
  "amount_captured": 0,
  "amount_refunded": 0,
  "card": {"brand":"visa","last4":"4242"},
  "failure_reason": null,
  "created_at": "2026-10-24T10:00:00+00:00",
  "refunds": []
}
```

The ID and timestamp above are illustrative.
Automatic capture returns `captured` after approval and credits the merchant ledger account.
Manual authorization posts no money until capture.

HTTP 201 means the payment resource has a definite result.
A card decline also returns 201, with `status: failed` and a `failure_reason`.
Exhausting gateways that definitely did not process likewise creates a failed payment with HTTP 201.
Read the resource state as well as the HTTP status.

HTTP 202 means the authorization outcome is unknown.
Keep the returned payment ID and poll `GET /payments/{payment_id}`.
Do not create a second charge while the original outcome is unresolved.
No eligible gateway returns `503 no_gateway_available` and leaves the request key unused.

The owning merchant can fetch the payment and all its refunds.
Another merchant receives the same 404 as an unknown ID.
Stored create or capture responses remain historical snapshots; GET returns current state.

## Capture and void

Capture uses a fresh operation key:

```http
POST /payments/11111111-1111-4111-8111-111111111111/capture
Authorization: Bearer <merchant_api_key>
Idempotency-Key: capture-order-1
Content-Type: application/json

{"amount_minor":40000}
```

Omit the body, send `{}`, or use a null amount to capture the full authorization.
An explicit amount must satisfy the same integer limits and not exceed the authorization.
One payment supports one capture, including a partial capture.
The unused authorization cannot be captured later.

Void uses `POST /payments/{payment_id}/void`, a new request key, and no body.
It only applies to an authorized payment.
Captured payments need refunds instead.

Approval returns 200. Unknown capture or void returns 202.
A definite gateway refusal returns `402 processor_declined` and leaves the payment authorized.
A definite failure to process returns `503 gateway_unavailable`, releases the operation key, and permits a same-key retry.
Changing from an omitted capture amount to an explicit amount changes the request fingerprint.

## Refund

```http
POST /payments/11111111-1111-4111-8111-111111111111/refunds
Authorization: Bearer <merchant_api_key>
Idempotency-Key: refund-order-1-part-1
Content-Type: application/json

{"amount_minor":15000}
```

Refunds require a captured payment.
The available amount is captured minus completed refunds minus pending refund reservations.
A payment can have several partial refunds.
Its state becomes `refunded` when completed refunds equal the captured amount.

The response contains `id`, `payment_id`, `amount_minor`, `status`, `failure_reason`, and `created_at`.
An approved refund returns 201 with `status: succeeded`.
A definite refund decline returns 201 with `status: failed`.
An unknown outcome returns 202 with `status: pending`; the amount stays reserved.
A definite failure to process returns 503 and releases the amount and operation key.
Refund history remains visible through the payment GET endpoint.

## Administrative ledger routes

Create an account with `{"owner_id":"customer-a","currency":"INR"}`.
Owner IDs must contain 1 to 64 characters.
The prefixes `system:` and `merchant:` are reserved.
Repeated account creation creates separate accounts for ordinary owners.

Top up an account with `{"amount_minor":10000}` and a request key.
The matching system funding account supplies the offsetting ledger entry.
System accounts cannot be topped up through this route.

A transfer request has this shape:

```json
{
  "from_account_id": "22222222-2222-4222-8222-222222222222",
  "to_account_id": "33333333-3333-4333-8333-333333333333",
  "amount_minor": 2500,
  "currency": "INR"
}
```

Accounts must differ, exist, and hold the requested currency.
Transfers cannot draw from a system account through the public administrative route.
An insufficient balance returns 422 with a transfer resource whose status is `FAILED`.
That result is stored and replayed, even if the account later receives funds.
Use a new operation key for a new spending attempt.

Account responses contain `id`, `owner_id`, `currency`, `balance_minor`, and `created_at`.
Transfer responses contain IDs, amount, currency, uppercase `status`, failure reason, and creation time.
`GET /accounts/{id}/entries?limit=50` returns an `entries` array, newest ID first.
Limits range from 1 through 200. Entries contain signed amounts and transfer IDs.

## Health, reconciliation, and recovery

`GET /health` returns `{"postgres":"ok","redis":"ok"}` when both checks pass.
Any failing check returns 503 with that dependency marked `down`.

Reconciliation returns `{"ok":true,"mismatches":[]}` when its checks agree.
Mismatches still return HTTP 200; inspect `ok` and `mismatches`.
This verifies internal accounting, rather than comparing records with a bank statement.

Recovery accepts `older_than_sec` as an optional query parameter.
Without it, recovery uses `RECOVERY_AFTER_SEC`.
The response counts completed `authorizations`, `captures`, `voids`, and `refunds`.
Zero counts can mean no eligible work, an active lease, or an unresolved gateway outcome.
The [gateway design](design/gateways.md) explains reference reuse and lookup behavior.

## Demo routes

These routes exist only when `DEMO_ENABLED=true`. `GET /` serves the page that calls them.
The [demo design](design/demo.md) explains the session model.

| Method | Path | Access | Normal result |
| --- | --- | --- | --- |
| POST | `/demo/session` | Same origin | 201 new session cookie, or 200 for a live session |
| DELETE | `/demo/session` | Same origin | 200; ends the session and clears the cookie so the walkthrough can restart |
| GET | `/demo/state` | Demo session | 200 snapshot |
| POST | `/demo/payments` | Demo session and same origin | 201 authorized INR 500.00 payment |
| POST | `/demo/payments/{payment_id}/capture` | Demo session and same origin | 200 payment after capturing INR 400.00 |
| POST | `/demo/payments/{payment_id}/refund` | Demo session and same origin | 201 refund of INR 150.00 |

The session is an HttpOnly, SameSite=Strict cookie named `paysentry_demo`, scoped to `/demo` for one hour.
POST and DELETE requests need an `Origin` header that matches the request host and scheme. Production requires `https`.
Bodies are optional. Any field, such as a card or amount, returns 422.

Each action uses a fixed request key, so a repeat returns the stored result with `Idempotent-Replayed: true`.
Action responses contain `result` (the payment or refund), `replayed`, and `snapshot`.
The snapshot holds `payment`, the INR `balance_minor`, the session's `ledger` entries, and three `checks`:
`balances_match`, `transfers_balance`, and `payments_match`.
Ledger entries name the account as `Your merchant` or `Gateway clearing`.

A missing, expired, or pre-restart session returns 401. A foreign origin returns 403.
A payment from another session returns 404.
Rate limits return 429. The daily session cap and an unreachable Redis return 503, since demo limits fail closed.

## Error contract

Application errors usually use this envelope:

```json
{"error":{"code":"invalid_state","message":"Payment is captured"}}
```

| HTTP status | Error code | Meaning |
| --- | --- | --- |
| 400 | `missing_idempotency_key` | Missing key or key outside the allowed length |
| 401 | `unauthorized` | Missing or wrong administrator or merchant credential |
| 402 | `processor_declined` | Capture or void was refused; includes `decline_code` |
| 403 | `system_account_not_allowed` | Forbidden administrative operation on a system account |
| 404 | `not_found` | Unknown account, transfer, or merchant-owned payment |
| 409 | `request_in_progress` | Same merchant request is still pending |
| 409 | `invalid_state` | Payment cannot perform this transition |
| 422 | `idempotency_key_reused` | Key was used with another request |
| 422 | `currency_mismatch` | Account currencies do not match the transfer |
| 422 | `capture_exceeds_authorized` | Capture exceeds authorization |
| 422 | `refund_exceeds_available` | Refund exceeds unreserved refundable amount |
| 429 | `rate_limited` | Retry later; response includes `retry_after_ms` and `Retry-After` |
| 503 | `gateway_unavailable` | Operation definitely was not processed; behavior depends on operation |
| 503 | `no_gateway_available` | No eligible gateway can accept a new payment |
| 503 | `service_unavailable` | Rate limiter unavailable with fail-open disabled |

Validation errors instead use `{"detail":[{"type":"...","loc":["body","amount_minor"],"msg":"..."}]}`.
Rejected input values are removed to avoid echoing card details.
Unexpected failures can still produce framework HTTP 500 responses.
See [app/errors.py](../app/errors.py) for the implemented error classes.
