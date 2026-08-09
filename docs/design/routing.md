# Routing

How PaySentry picks a gateway for each payment, and what it does when that gateway fails. Keep this file in step with the code. Connectors and outcomes are in [gateways.md](gateways.md).

## Overview

```
POST /payments ──> router.plan(currency, brand)
                     1. rules: which gateways can take this payment
                     2. circuit breakers: drop gateways that are failing hard
                     3. ranking: success rate (Thompson sampling) or fixed priority
                   ──> try gateways in that order
                     - approved or declined: stop
                     - unknown: stop, answer 202, let recovery settle it
                     - not processed: try the next gateway (failover)
                   ──> router.record(gateway, outcome)
```

The router lives in `app/routing.py`. Its state (success windows and breaker state) is in Redis, so every API instance shares one view of gateway health.

Routing applies to authorizations. Captures, voids and refunds must go to the gateway that holds the authorization, so they never route.

## 1. Rules

Each gateway declares what it accepts in `GATEWAYS`:

```json
{"beta": {"flavour": "beta", "url": "...", "currencies": ["INR", "USD"], "brands": ["visa", "mastercard"]}}
```

A gateway is eligible when it accepts both the payment's currency and its card brand. Leaving out `currencies` or `brands` means "all".

If no gateway is eligible, or every eligible one has an open circuit, the API answers `503 no_gateway_available` before claiming the idempotency key. Nothing was attempted, so the same key can retry.

## 2. Circuit breakers

A gateway that keeps failing at the network level gets skipped for a while, instead of costing every payment a wasted call.

| State | Meaning | Next |
|---|---|---|
| Closed | Normal | Opens after `CIRCUIT_FAILURE_THRESHOLD` (5) errors in a row |
| Open | Skipped | After `CIRCUIT_COOLDOWN_SEC` (30) becomes half-open |
| Half-open | One probe request allowed | Probe succeeds: closed. Probe fails: open again |

Only `not_processed` and `unknown` count as errors. A decline proves the gateway is up and answering, so it resets the error streak. The half-open probe is a Redis `SET NX` with an expiry, so across all API instances exactly one payment probes.

## 3. Ranking

`ROUTING_STRATEGY` picks one of two:

- **`priority`**: the order gateways appear in `GATEWAYS`. Simple and predictable. Used in most tests.
- **`success_rate`** (default): each gateway keeps its last `ROUTING_WINDOW` (100) authorization outcomes in Redis. A success is an approval; declines and errors are failures. For each payment, the router draws one sample per gateway from `Beta(successes + 1, failures + 1)` and tries the gateways in order of their samples.

Thompson sampling handles the trade-off between using the best gateway and checking the others. A gateway at 95% wins almost every draw. A gateway at 40% still wins now and then, so when it recovers, its window fills with fresh successes and its share grows back without a manual reset. A brand new gateway starts at `Beta(1, 1)`, an even guess, so it gets traffic straight away.

Declines count against a gateway on purpose. Different gateways get different approval rates for the same cards (issuer relationships, retry logic, 3DS handling), and that difference is what success-rate routing exists to exploit.

## Failover

Failover happens only on `not_processed`, where the gateway certainly did nothing. Then the next gateway in the plan gets a new attempt, up to `ROUTING_MAX_ATTEMPTS` (3).

| Outcome | Failover? | Why |
|---|---|---|
| `approved` | No | Done |
| `declined` | No | The card was refused. Retrying elsewhere is a product decision (soft-decline retries) this project does not make |
| `unknown` | **Never** | The first gateway may have charged. A second attempt could charge the customer twice |
| `not_processed` | Yes | Nothing happened, so trying elsewhere is safe |

Each try is its own `payment_attempts` row with its own reference. When failing over, the previous attempt is marked `failed` and the payment's `gateway` moves to the new one in the same transaction. `finish_authorization` sets the payment's gateway from the attempt that settled it. So captures and refunds always go where the money is.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `ROUTING_STRATEGY` | `success_rate` | `success_rate` or `priority` |
| `ROUTING_WINDOW` | `100` | Outcomes remembered per gateway |
| `ROUTING_MAX_ATTEMPTS` | `3` | Gateways tried per payment, counting failover |
| `ROUTING_SEED` | unset | Seeds Thompson sampling, for repeatable tests and simulations |
| `CIRCUIT_FAILURE_THRESHOLD` | `5` | Errors in a row that open a circuit |
| `CIRCUIT_COOLDOWN_SEC` | `30` | How long a circuit stays open before a probe |

## Simulation

`scripts/simulate_routing.py` sends payments through the real app, with real Postgres and Redis, to three in-process mock gateways. It changes the gateways' health in four phases, and runs once with `priority` and once with `success_rate`. Results are in the "Results" section below once the script has run.

## Tests

| File | Covers |
|---|---|
| `tests/test_routing.py` | Rules by currency and brand, failover on `not_processed` only, no failover on declines or unknown outcomes, all gateways down, circuit opening, half-open probe success and failure, success-rate routing moving traffic to the healthier gateway, exploration when gateways are equal |
