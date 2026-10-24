# PaySentry

[![tests](https://github.com/vaibhavshiroorkar/paysentry/actions/workflows/ci.yml/badge.svg)](https://github.com/vaibhavshiroorkar/paysentry/actions/workflows/ci.yml)

PaySentry takes card payments for merchants and records every paisa in a double-entry ledger.
The portfolio demo uses synthetic cards and mock gateways.

I built it to learn how to charge a card exactly once. Clients retry. Two copies of a request arrive at the same moment. A gateway times out and you can't tell if it charged. The code handles each of these, and each one has a test that runs against a real Postgres.

Python, FastAPI, Postgres (plain SQL through asyncpg) and Redis.

## Run it

You need Docker and [uv](https://docs.astral.sh/uv/).

```sh
docker compose up --build
```

This starts the API on port 8000, Postgres, Redis and three mock card gateways. API docs are at `http://localhost:8000/docs`.

For local Python, PowerShell examples, configuration, and schema upgrade limits, see the [development guide](docs/development.md).
Keep valuable data out of the disposable development databases.

## Try a payment

From a second terminal (bash, with `jq`):

```sh
KEY=$(curl -s localhost:8000/merchants -H 'X-Admin-Key: dev-admin-key' \
  -H 'content-type: application/json' -d '{"name":"Acme"}' | jq -r .api_key)
AUTH="Authorization: Bearer $KEY"

# Authorize 500 rupees without capturing.
P=$(curl -s localhost:8000/payments -H "$AUTH" -H 'Idempotency-Key: order-1' \
  -H 'content-type: application/json' \
  -d '{"amount_minor":50000,"currency":"INR","capture_method":"manual",
       "card":{"number":"4242424242424242","exp_month":12,"exp_year":2030,"cvc":"123"}}' | jq -r .id)

# Capture 400, then refund 150. Run either line twice: the second call is replayed, not repeated.
curl -s localhost:8000/payments/$P/capture -H "$AUTH" -H 'Idempotency-Key: cap-1' \
  -H 'content-type: application/json' -d '{"amount_minor":40000}'
curl -s localhost:8000/payments/$P/refunds -H "$AUTH" -H 'Idempotency-Key: ref-1' \
  -H 'content-type: application/json' -d '{"amount_minor":15000}'

curl -s localhost:8000/balance -H "$AUTH"
curl -s localhost:8000/internal/reconciliation -H 'X-Admin-Key: dev-admin-key'
```

Other test cards (declines, failing refunds) are listed in [payments.md](docs/design/payments.md#test-cards).

## Run the tests

```sh
docker compose up -d db redis
uv run python -m pytest
```

The tests make their own disposable `paysentry_test` database.
Application clients check reconciliation at teardown, except when a test deliberately inspects a mismatch.
A few of the things they fire at the API:

- 50 copies of one transfer at once. One transfer happens, and the other 49 get the stored answer.
- 50 spends of 30 from a balance of 100. Exactly 3 go through.
- 100 transfers A->B and B->A at once. None deadlock.
- 20 refunds of 100 against a 1000 capture. Exactly 10 go through.
- A gateway timeout or a crash in the middle of every operation. A background job asks the gateway what happened and finishes the payment once.

## How it works

Start with [Building PaySentry](docs/build-story.md) for the full development story.
The [documentation index](docs/README.md) links the setup guide, architecture, API reference, operations, and tests.
The [study guide](docs/guide.md) teaches the mechanisms with questions and exercises.
The design docs go into more detail:

- [payments.md](docs/design/payments.md): the payment lifecycle, idempotency keys and card data
- [gateways.md](docs/design/gateways.md): talking to gateways, and what to do when one doesn't answer
- [routing.md](docs/design/routing.md): picking a gateway, circuit breakers and failover

## Layout

```
app/
  ledger.py       transfers: claim the key, lock rows, write entries
  payments.py     create, capture, void, refund
  connectors/     one per gateway API
  recovery.py     settles payments whose gateway outcome is unknown
  routing.py      rules, circuit breakers, success-rate ranking
  contextual.py   LinUCB routing model
  schema.sql      tables, constraints, append-only trigger
  routes/         HTTP layer
gateway/          mock card gateway
tests/
scripts/
  simulate_routing.py
```

## History

`prototype/` is the first version, from January and February 2026.
It began as a fraud detection idea and reached a small transactions API.
The recorded rebuild milestones run from February through October: ledger, merchant payments, gateway recovery, and routing.
The [build story](docs/build-story.md) ties each stage to its existing commits and implementation.

## Next

The [roadmap](docs/roadmap.md) covers the proposed work and completion checks.
Start with database migrations, then merchant webhooks through a transactional outbox.
Monitoring, load tests, merchant tools, and a demo follow.
