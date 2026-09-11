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

If no gateway is eligible, or every eligible one has an open circuit, the API answers `503 no_gateway_available` and rolls back its claim on the idempotency key. Nothing was attempted, so the same key can retry. The check runs after the replay check: a retry of a payment that already completed gets its stored response, whatever the gateways look like now.

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

`scripts/simulate_routing.py` sends payments through the real app, with real Postgres and Redis, to three in-process mock gateways. It changes the gateways' health in four phases:

1. All healthy: alpha declines 5%, beta 8%, gamma 10%.
2. Alpha declines 60%.
3. Alpha answers `503` to everything.
4. Alpha back to 5%.

It runs each phase under three setups, and checks that the ledger reconciles after each one.

```sh
docker compose up -d db redis
uv run python scripts/simulate_routing.py 1000
```

### Results

1000 payments per phase, 10 at a time, from one run on 2026-10-11. Runs vary by about a point, because concurrent payments draw from the gateways' random sequences in a different order each time.

Authorization success rate:

| Phase | Fixed priority, no breaker | Fixed priority + breaker | Success rate + breaker |
|---|---|---|---|
| 1. All healthy | 93.4% | 93.4% | 91.0% |
| 2. Alpha declines 60% | 42.9% | 42.9% | 89.7% |
| 3. Alpha down (503) | 91.1% | 91.1% | 90.3% |
| 4. Alpha recovered | 95.2% | 91.8% | 92.2% |

Gateway calls per payment:

| Phase | Fixed priority, no breaker | Fixed priority + breaker | Success rate + breaker |
|---|---|---|---|
| 1. All healthy | 1.00 | 1.00 | 1.00 |
| 2. Alpha declines 60% | 1.00 | 1.00 | 1.00 |
| 3. Alpha down (503) | 2.00 | 1.01 | 1.00 |
| 4. Alpha recovered | 1.00 | 1.00 | 1.00 |

What the numbers show:

- **Degradation is where success-rate routing earns its place.** Fixed priority keeps sending everything to alpha and approves 42.9%. Success-rate routing moves traffic to beta and gamma within a few dozen payments and holds 89.7%. Failover cannot help here: a decline is an answer, not an outage.
- **Outages are where the breaker earns its place.** Failover alone keeps payments succeeding (91.1%), but every payment first spends a call on the dead gateway: 2.00 calls per payment. With the breaker, alpha is skipped after 5 errors: 1.01.
- **Exploration has a cost.** With all gateways healthy, success-rate routing approves 91.0% against 93.4%, because it keeps sending some traffic to beta and gamma to learn how they are doing. That is the price of noticing phase 2 at all.
- **The breaker is slow to notice a recovery.** In phase 4, both breaker setups still route around alpha, because each phase lasts about 10 seconds and the cooldown is 30. Until the probe, the breaker cannot know alpha is back. A shorter cooldown trades this against more probes into a dead gateway.

## Tests

| File | Covers |
|---|---|
| `tests/test_routing.py` | Rules by currency and brand, failover on `not_processed` only, no failover on declines or unknown outcomes, all gateways down, no eligible gateway, circuit opening, half-open probe success and failure, the probe key expiring, success-rate routing moving traffic to the healthier gateway, old outcomes leaving the window, exploration when gateways are equal, Redis down |

Mutation check, run by hand during the build: allowing failover after an `unknown` outcome fails the no-failover-on-unknown test.
