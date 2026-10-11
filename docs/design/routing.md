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

`ROUTING_STRATEGY` picks one of three:

- `priority` tries gateways in the order they appear in `GATEWAYS`. It is simple and predictable, and most tests use it.
- `contextual` learns which gateway is best for this kind of payment. See [Contextual ranking](#contextual-ranking) below.
- `success_rate` is the default. Each gateway keeps its last `ROUTING_WINDOW` (100) authorization outcomes in Redis. A success is an approval; declines and errors are failures. For each payment, the router draws one sample per gateway from `Beta(successes + 1, failures + 1)` and tries the gateways in order of their samples.

Thompson sampling handles the trade-off between using the best gateway and checking the others. A gateway at 95% wins almost every draw. A gateway at 40% still wins now and then, so when it recovers, its window fills with fresh successes and its share grows back without a manual reset. A brand new gateway starts at `Beta(1, 1)`, an even guess, so it gets traffic straight away.

Declines count against a gateway on purpose. Different gateways get different approval rates for the same cards (issuer relationships, retry logic, 3DS handling), and that difference is what success-rate routing exists to exploit.

### Contextual ranking

`success_rate` asks "which gateway is best overall?". But gateways differ by segment: one may approve visa well and mastercard badly, another may struggle with large amounts. A single success rate per gateway averages those differences away. `contextual` asks "which gateway is best for this payment?".

The model is LinUCB, a contextual bandit (Li et al., 2010). Each payment becomes a feature vector `x` of 15 numbers, all 0 or 1:

| Features | Values |
|---|---|
| Bias | always 1 |
| Card brand | visa, mastercard, amex, other |
| Amount bucket | under 500, 500 to 5,000, 5,000 to 50,000, over 50,000 (major units) |
| Currency | INR, USD |
| Brand x large amount | one per brand, set when the amount is 5,000 or more |

Each gateway keeps the statistics of a ridge regression from features to approval (1 approved, 0 otherwise):

```
A = lambda * I + sum of x xT      (15 x 15)
b = sum of reward * x             (15)
theta = A^-1 b                    estimated approval weights
score = thetaT x + alpha * sqrt(xT A^-1 x)
```

Rewards are centred: an approval counts +0.5 and anything else -0.5. Ridge regression pulls a gateway with no data toward 0, so centring makes "no data" mean a 50% guess rather than 0%. Uncentred, a gateway that has declined everything would score the same as one never tried. The mutation check below caught exactly that.

The first term is the gateway's predicted approval rate for this payment (minus 0.5). The second is an exploration bonus: large when the gateway has seen few payments like this one, small when it has seen many. The router tries gateways in order of score. A gateway with little data in a segment gets tried there, and one that is known to be bad there does not.

Because features are shared, the model generalizes. A gateway that declines mastercard learns a negative mastercard weight from small and large payments alike, so it does not have to fail in every brand-and-amount combination separately.

#### Forgetting

Gateways change, so old evidence has to fade. Every routing decision increments a global counter in Redis. A gateway's statistics are multiplied by `gamma ^ (decisions since its last update)` (`ROUTING_DECAY`, default 0.998) before each read and update. Two things follow:

- A gateway that degrades loses its good reputation within a few hundred payments.
- A gateway that gets no traffic slowly loses its *bad* reputation too: its statistics shrink toward `lambda * I`, its exploration bonus grows, and eventually it is tried again. That is how a recovered gateway gets noticed without a manual reset.

Decay is counted in routing decisions, not seconds. That ties forgetting to traffic: a quiet night forgets nothing, and the simulation behaves the same at any speed.

#### Shared state

Each gateway's `A`, `b` and last-update counter live in one Redis key. A Lua script applies the decay and adds the new observation atomically, so concurrent payments on several API instances never lose an update. Reads fetch all gateways in one round trip, and NumPy solves the 15 x 15 systems in microseconds.

The success windows and circuit breakers keep running under `contextual` too. Breakers still drop dead gateways before ranking.

## Failover

Failover happens only on `not_processed`, where the gateway certainly did nothing. Then the next gateway in the plan gets a new attempt, up to `ROUTING_MAX_ATTEMPTS` (3).

| Outcome | Failover? | Why |
|---|---|---|
| `approved` | No | Done |
| `declined` | No | The card was refused. Retrying elsewhere is a product decision (soft-decline retries) this project does not make |
| `unknown` | Never | The first gateway may have charged. A second attempt could charge the customer twice |
| `not_processed` | Yes | Nothing happened, so trying elsewhere is safe |

Each try is its own `payment_attempts` row with its own reference. When failing over, the previous attempt is marked `failed` and the payment's `gateway` moves to the new one in the same transaction. `finish_authorization` sets the payment's gateway from the attempt that settled it. So captures and refunds always go where the money is.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `ROUTING_STRATEGY` | `success_rate` | `priority`, `success_rate` or `contextual` |
| `ROUTING_ALPHA` | `0.2` | Contextual: weight of the exploration bonus |
| `ROUTING_LAMBDA` | `1.0` | Contextual: ridge regularization, the prior that keeps `A` invertible |
| `ROUTING_DECAY` | `0.998` | Contextual: factor applied per routing decision |
| `ROUTING_WINDOW` | `100` | Outcomes remembered per gateway |
| `ROUTING_MAX_ATTEMPTS` | `3` | Gateways tried per payment, counting failover |
| `ROUTING_SEED` | unset | Seeds Thompson sampling, for repeatable tests and simulations |
| `CIRCUIT_FAILURE_THRESHOLD` | `5` | Errors in a row that open a circuit |
| `CIRCUIT_COOLDOWN_SEC` | `30` | How long a circuit stays open before a probe |

## Simulation

`scripts/simulate_routing.py` sends payments through the real app, with real Postgres and Redis, to three in-process mock gateways. It checks that the ledger reconciles after every setup. It runs two scenarios.

**Outages.** One kind of card. Alpha's health changes in four phases:

1. All healthy: alpha declines 5%, beta 8%, gamma 10%.
2. Alpha declines 60%.
3. Alpha answers `503` to everything.
4. Alpha back to 5%.

**Segments.** Mixed traffic: 50% visa, 40% mastercard, 10% amex; 70% of amounts under 5,000, the rest up to 60,000. Each gateway is good at different payments:

| Gateway | visa | mastercard | amex | 5,000 and over |
|---|---|---|---|---|
| alpha | 5% declined | 45% | 10% | at least 40% |
| beta | 35% | 6% | 50% | no change |
| gamma | 12% | 12% | 12% | no change |

In phase 2, alpha's visa declines rise to 50%.

```sh
docker compose up -d db redis
uv run python scripts/simulate_routing.py 1000
```

### Results

From one run on 2026-10-11 with the default settings. Runs vary by about a point, because concurrent payments draw from the gateways' random sequences in a different order each time.

**Outages**, 1000 payments per phase, 10 at a time. Authorization success rate:

| Phase | Fixed priority, no breaker | Fixed priority + breaker | Success rate + breaker | Contextual + breaker |
|---|---|---|---|---|
| 1. All healthy | 93.4% | 93.4% | 91.0% | 93.4% |
| 2. Alpha declines 60% | 42.9% | 42.9% | 89.7% | 86.8% |
| 3. Alpha down (503) | 91.1% | 91.1% | 90.3% | 91.4% |
| 4. Alpha recovered | 95.2% | 91.8% | 92.2% | 93.9% |

Gateway calls per payment:

| Phase | Fixed priority, no breaker | Fixed priority + breaker | Success rate + breaker | Contextual + breaker |
|---|---|---|---|---|
| 1. All healthy | 1.00 | 1.00 | 1.00 | 1.00 |
| 2. Alpha declines 60% | 1.00 | 1.00 | 1.00 | 1.00 |
| 3. Alpha down (503) | 2.00 | 1.00 | 1.00 | 1.00 |
| 4. Alpha recovered | 1.00 | 1.00 | 1.00 | 1.00 |

**Segments**, 2000 payments per phase, 10 at a time. Authorization success rate:

| Phase | Fixed priority | Success rate | Contextual (LinUCB) | Best possible |
|---|---|---|---|---|
| 1. Gateways differ by segment | 73.6% | 86.6% | 89.7% | 93.0% |
| 2. Alpha worsens on visa | 54.1% | 86.2% | 88.4% | 90.5% |

"Best possible" is computed, not measured: every segment sent to its best gateway, with no learning or exploration. With one gateway per payment, nothing can beat it.

What I read from these numbers:

- When a gateway degrades, learned routing helps a lot. Fixed priority keeps sending everything to alpha and approves 42.9%. Both learned strategies move traffic away. Failover cannot help here, because a decline is an answer, not an outage.
- When a gateway is down, the breaker is what helps. Failover alone keeps payments succeeding (91.1%), but every payment first wastes a call on the dead gateway, so it costs 2.00 calls per payment. With the breaker, alpha is skipped after 5 errors.
- When gateways differ by segment, contextual routing wins. Success-rate routing can only pick the best gateway overall (gamma), so it tops out near gamma's 88%. Contextual routing sends visa to alpha and mastercard to beta. That gains about 3 points in phase 1 and 2 in phase 2. It still ends 2 to 3 points short of the best possible, which is what learning and exploring cost.
- Contextual routing reacts more slowly to a sudden drop. In the outage scenario's phase 2 it approves 86.8% against success rate's 89.7%. A decay of 0.998 per decision remembers about 500 decisions, so 300 payments of good history take a while to fade. Success rate's 100-outcome window forgets faster. The same long memory means it explores less when all is well (93.4% against 91.0% in phase 1), and it helps it find alpha again after recovery (93.9% in phase 4).
- The breaker is slow to notice a recovery. Each phase lasts about 10 seconds and the cooldown is 30, so in phase 4 the priority setup still routes around alpha. The breaker cannot know alpha is back until its probe goes through.

### How the contextual defaults were chosen

The first version used brand, amount and currency features only, with `alpha = 0.5` and `decay = 0.995`. It reached 88.4% and 86.0% in the segments scenario, well short of the best possible 93.0% and 90.5%. Two changes closed most of that gap:

1. Less exploration. With 3 gateways and dozens of segments, `alpha = 0.2` explores less and reached 89.9% and 87.4%.
2. Cross features. A linear model adds feature effects together, so it cannot say "alpha is fine for visa, except large visa payments". Four brand-by-large-amount features give it a weight for exactly that. With `decay = 0.998` as well, the tuning run reached 90.8% and 88.9%.

These values were tuned on this simulation, so they fit its gateways. Real traffic would need its own tuning, ideally on replayed production data.

## Tests

| File | Covers |
|---|---|
| `tests/test_routing.py` | Rules by currency and brand, failover on `not_processed` only, no failover on declines or unknown outcomes, all gateways down, no eligible gateway, circuit opening, half-open probe success and failure, the probe key expiring, success-rate routing moving traffic to the healthier gateway, old outcomes leaving the window, exploration when gateways are equal, Redis down |
| `tests/test_contextual.py` | Feature encoding, learning per brand, carrying weights to unseen amounts, forgetting after a regime change, idle gateways explored again, decay underflow, atomic concurrent updates, Redis down |

Mutation checks, run by hand during the build:

- Allowing failover after an `unknown` outcome fails the no-failover-on-unknown test.
- Turning decay off fails the regime-change and idle-gateway tests.
- Removing the exploration bonus fails the idle-gateway test. Before rewards were centred, it did not: a gateway that declined everything scored the same as one never tried, so the test passed for the wrong reason.
