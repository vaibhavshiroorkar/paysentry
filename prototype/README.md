# PaySentry prototype (January to February 2026)

This is the first version of PaySentry, kept for the record. It is not maintained, and the main project at the repository root does not use any of it.

## What I was aiming for

The plan was a fraud detection service for payments, built in three stages:

1. A transaction API with a ledger and a merchant dashboard.
2. Fraud scoring with XGBoost and Isolation Forest, graph analysis with NetworkX to find rings of linked accounts, and SHAP to explain each decision.
3. An LLM agent that could answer compliance questions over RBI and NPCI circulars.

## What actually got built

Only the start of stage 1:

- A FastAPI app with `POST /transactions` and `GET /transactions/{id}`, stored in Postgres through async SQLAlchemy.
- `users` and `transactions` tables. Each transaction has a `risk_score` column, but nothing ever sets it.
- Pydantic schemas for users, merchants and transactions.
- A Docker Compose file for Postgres and pgAdmin.

`legacy/` holds scratch files from learning the basics: dataclasses, decorators, async generators, and one call to an LLM through Groq. `legacy/conflict.txt` is left over from practising merge conflicts.

Some choices here are ones I would not make again. Amounts are stored as `Float`, which cannot hold most decimal amounts exactly. There is no idempotency key, so a retried request creates a second transaction. Those two problems are where the rebuild started.

## Running it

You need Docker and [uv](https://docs.astral.sh/uv/). Make a `.env` in this folder:

```sh
POSTGRES_USER=paysentry
POSTGRES_PASSWORD=paysentry
POSTGRES_DB=paysentry
PGADMIN_DEFAULT_EMAIL=admin@example.com
PGADMIN_DEFAULT_PASSWORD=admin
```

Then, from `prototype/`:

```sh
docker compose up -d
uv sync
uv run uvicorn app.main:app --reload
```

The app does not create its tables on startup, so `POST /transactions` fails until you create `users` and `transactions` yourself. API docs are at `http://127.0.0.1:8000/docs`.
