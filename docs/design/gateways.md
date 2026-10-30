# Gateways and unknown outcomes

How PaySentry talks to card gateways, and what it does when it cannot tell whether a gateway acted. Keep this file in step with the code. The payment state machine it plugs into is in [payments.md](payments.md).

## Overview

```
payments service --> connector (one per gateway API) --HTTP--> mock gateway service
       |                    |
       |                    +-- classifies every call: approved, declined, not processed, unknown
       |
       +-- recovery job --> asks the gateway what happened to anything left in flight
```

- **Mock gateways** (`gateway/`) are a separate FastAPI service. Docker Compose runs three of them, in two API styles. Tests run the same app in-process.
- **Stripe** in test mode is an optional real gateway. See [Stripe](#stripe).
- **Connectors** (`app/connectors/`) translate our four operations (authorize, capture, void, refund) into each gateway's API, and translate the answers back.
- **Recovery** (`app/recovery.py`) resolves operations whose outcome is unknown. It runs on a timer inside the API process, and admins can trigger it with `POST /internal/recovery/run`.

## The four outcomes

Every gateway call ends in exactly one of these:

| Outcome | Meaning | Examples |
|---|---|---|
| `approved` | The gateway did it | 200 with an approved status |
| `declined` | The gateway refused, for good | 200 with a declined status |
| `not_processed` | The gateway certainly did nothing | Connection refused, `503` (the gateways promise 503 means nothing was processed) |
| `unknown` | The gateway may or may not have acted | Our deadline passed after sending, connection dropped mid-response, `500`, `502`, `504` |

Most of this document depends on telling `not_processed` from `unknown`. After `not_processed` it is safe to give up, or to try another gateway ([routing.md](routing.md#failover)). After `unknown`, both of those can double charge, because the first gateway may have taken the money. The only safe move is to find out what it did.

## Every request carries our reference

Each authorization is a `payment_attempts` row. Its id is sent to the gateway as our reference. Captures and voids send `capture:<payment id>` and `void:<payment id>`. Refunds send the refund id.

The mock gateways are idempotent on these references, as real gateways are. A repeated request with the same reference returns the original result and never acts twice. That makes two recovery moves safe:

- **Look up** an authorization by reference.
- **Re-send** a capture, void or refund with the same reference.

Authorizations are looked up, never re-sent, because re-sending needs the card number and PaySentry does not keep it.

## Unknown outcomes on the request path

| Operation | On `unknown` |
|---|---|
| Create payment | Attempt marked `unknown`. Payment stays `processing`. Response `202` with the payment body |
| Capture, void | Payment stays `capture_pending` / `void_pending`. Response `202` |
| Refund | Refund stays `pending`. Response `202` with the refund body |

A `202` is not stored under the idempotency key. The key stays `in_progress`, so a retry gets `409 request_in_progress` until recovery settles the operation. Recovery then stores the final response under the key. From then on, a retry gets the real outcome. The client can also poll `GET /payments/{id}`.

To make this possible, payments, in-flight operations and refunds remember the idempotency key that started them.

## Recovery

The recovery job looks for work that has been in flight longer than `RECOVERY_AFTER_SEC` (default 60):

| Stuck in | Action | Result |
|---|---|---|
| Attempt `pending` or `unknown`, payment `processing` | Look up the charge by reference | Found: finish as authorized, captured or failed. Not found: payment `failed` with `gateway_no_record` |
| Payment `capture_pending` | Re-send the capture with the same reference | Finish as captured, or back to authorized |
| Payment `void_pending` | Re-send the void | Finish as voided, or back to authorized |
| Refund `pending` | Re-send the refund | Finish as succeeded or failed |

If the gateway's answer is still `unknown`, the job leaves the row for the next run.

`pending` covers the crash case. If the process dies after transaction A and before transaction B, the attempt never got an outcome, and recovery treats it the same as `unknown`.

### Why recovery waits 60 seconds

"Not found" is only trustworthy once the original request can no longer arrive at the gateway. The gateway deadline is 2 seconds, so a request still in flight after a minute is not a realistic case. Looking up too early could mark a payment failed just before the gateway charges it.

### Several recovery workers

Each worker claims rows with `FOR NO KEY UPDATE SKIP LOCKED` and sets a 30-second lease.
The claim commits before any gateway call.
Other workers skip locked or leased rows.
Finalizers check the pending status, so competing workers cannot finish the same operation twice.

### Shared finishing code

The request path and recovery call the same `finish_*` functions in `app/payments.py`. A payment recovered after a timeout ends in exactly the state it would have reached without one, with the same ledger posting and the same stored response.

## Connectors

`app/connectors/base.py` defines the interface:

```python
class Connector(Protocol):
    name: str
    async def authorize(self, *, reference, amount_minor, currency, card, capture) -> GatewayResult
    async def lookup(self, *, reference) -> GatewayResult  # an authorization by our reference
    async def capture(self, *, gateway_reference, reference, amount_minor, currency) -> GatewayResult
    async def void(self, *, gateway_reference, reference) -> GatewayResult
    async def refund(self, *, gateway_reference, reference, amount_minor, currency) -> GatewayResult
```

`GatewayResult` holds the outcome, the gateway's own id, the final charge status (`authorized` or `captured`) and a decline code.

Every call runs under one deadline (`GATEWAY_TIMEOUT_SEC`, default 2) with `asyncio.timeout`. Connect errors are `not_processed`, because the request never left. Anything that fails after the request was sent is `unknown`. The httpx connect timeout is half the deadline, so a connection that never opens is caught as a connect error before the deadline can turn it into `unknown`.

| | Alpha | Beta |
|---|---|---|
| Style | `/v1/charges`, snake case | `/api/payments`, camel case |
| Money | Integer minor units | Decimal string in major units (`"123.45"`) |
| Card | Nested object | Flat fields, expiry as `MM/YY` |
| Statuses | `authorized`, `captured`, `voided`, `declined` | `AUTHORISED`, `CAPTURED`, `CANCELLED`, `REFUSED` |

Beta's decimal strings are converted with `Decimal`, never `float`, and tested at the edges (`5` -> `"0.05"`, `100` -> `"1.00"`).

## Stripe

`app/connectors/stripe.py` runs the same four operations against Stripe's PaymentIntents API in test mode.

| Operation | Stripe call |
|---|---|
| Authorize | `POST /v1/payment_intents` with `confirm=true` and `capture_method` `manual` or `automatic` |
| Look up | `GET /v1/payment_intents`, newest first, matching `metadata[reference]` |
| Capture | `POST /v1/payment_intents/{id}/capture` with `amount_to_capture` |
| Void | `POST /v1/payment_intents/{id}/cancel` |
| Refund | `POST /v1/refunds` with the PaymentIntent and amount |

Every POST sends our reference as Stripe's `Idempotency-Key`, so a repeat returns the first answer and never acts twice.
Authorizations also carry the reference in `metadata[reference]` for lookup.

Stripe differs from the mocks in four ways:

- **Card numbers never reach Stripe.** Stripe refuses raw card numbers from a server that has not proven PCI compliance, test mode included. The connector maps each [test card](payments.md#test-cards) to Stripe's matching test PaymentMethod, such as `pm_card_visa`. Any other number is declined with `stripe_test_card_required` before a request is sent. A live integration would collect cards in the browser with Stripe's own form and send PaySentry a token.
- **A 503 is `unknown`.** Stripe does not promise that a 503 did nothing. `401`, `403` and `429` are `not_processed`: Stripe did nothing, so another gateway may try. `409` means another request with the same key is still running, so it is `unknown`. `402` is a decline and keeps Stripe's `decline_code`.
- **Lookup scans.** Stripe cannot look up a PaymentIntent by our reference. Its search API is eventually consistent and not offered in India, so lookup pages through the PaymentIntents created in the last two days, up to 1,000 of them. If it cannot finish the scan, the outcome stays `unknown` and recovery tries again.
- **Some failures arrive later.** Stripe reports a refund that fails after it was accepted, as `4000000000005126` does, through webhooks. PaySentry does not consume webhooks, so that refund stays `succeeded`. A card that needs 3D Secure is declined with `authentication_required`, because there is no browser to complete the challenge.

Stripe keeps idempotency keys for 24 hours. Recovery runs every few seconds, so it re-sends long before a key expires.

Only test mode keys are accepted. Settings fail at startup if a `stripe` gateway is configured without a `STRIPE_SECRET_KEY` that starts with `sk_test_` or `rk_test_`. The key goes only to the Stripe client, as a bearer token.

To route payments to Stripe first and fall back to a mock:

```sh
GATEWAYS='{"stripe": {"flavour": "stripe", "url": "https://api.stripe.com"}, "alpha": {"flavour": "alpha", "url": "http://localhost:9001"}}'
STRIPE_SECRET_KEY=sk_test_...
ROUTING_STRATEGY=priority
GATEWAY_TIMEOUT_SEC=10
```

Stripe confirms a card in about one to two seconds, so the 2-second default deadline is too short for it.

## Mock gateway behaviour

Each mock gateway keeps charges and refunds in memory, keyed by our reference. Its behaviour is set through `POST /_control`, or directly on the app object in tests:

| Setting | Effect |
|---|---|
| `latency_ms` | Wait before answering |
| `decline_rate` | Share of authorizations declined (`do_not_honor`) |
| `unavailable_rate` | Share of requests answered `503` without acting |
| `unknown_rate` | Share of requests that act, then hang for `hang_ms` |
| `seed` | Seeds the random draws, so runs repeat |

The [test cards](payments.md#test-cards) behave the same on every gateway.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `GATEWAYS` | `{"alpha": {"flavour": "alpha", "url": "http://localhost:9001"}, "beta": {"flavour": "beta", "url": "http://localhost:9002"}}` | JSON: each gateway's name, API flavour (`alpha`, `beta` or `stripe`) and URL |
| `STRIPE_SECRET_KEY` | empty | Stripe test mode secret key, required when a gateway uses the `stripe` flavour |
| `GATEWAY_TIMEOUT_SEC` | `2` | Deadline per gateway call |
| `RECOVERY_AFTER_SEC` | `60` | How long work must be stuck before recovery touches it |
| `RECOVERY_INTERVAL_SEC` | `10` | How often the recovery loop runs. `0` turns it off |

## Tests

| File | Covers |
|---|---|
| `tests/test_gateway_mock.py` | Mock gateway idempotency by reference, both API styles |
| `tests/test_connectors.py` | Outcome classification for every failure kind, Beta money conversion |
| `tests/test_stripe.py` | The Stripe connector against an in-memory fake Stripe (`tests/fake_stripe.py`): test card mapping, idempotent repeats, status codes, lookup across pages, test-key enforcement, and a payment through the whole app |
| `tests/test_payment_gateways.py` | Full payment lifecycle through both gateways |
| `tests/test_recovery.py` | Timeouts on every operation, crashes before and after the gateway call, recovery leaving fresh work alone, a lookup that itself times out, concurrent finishers, two recovery runs at once, a gateway removed from config, the background loop |

Crashes are simulated by `tests/transports.py`, which kills the first gateway request either before or after the gateway acts.

Mutation check, run by hand during the build: removing `status = 'processing'` from the update in `finish_authorization` fails the concurrent finishers test.
