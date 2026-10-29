# Hosting the demo

The [Render Blueprint](../render.yaml) runs the browser demo on Render's free plans.
It creates one web service, one Postgres database, and one Key Value instance.
The service processes synthetic payments through embedded mock gateways. It never takes real cards.

## Free tier limits

Check [Render's free tier page](https://render.com/docs/free) before applying the Blueprint, since limits change.
In October 2026 it listed these:

- The web service spins down after 15 minutes without traffic. The next request waits about a minute.
- Each workspace gets 750 free instance hours a month. Spun-down time does not count.
- Free Postgres expires 30 days after creation. After a further 14 days, Render deletes it and its data.
- Free Key Value keeps data only in memory. A restart clears it.
- A workspace can have one free Postgres database and one free Key Value instance at a time.

Every resource in the Blueprint sets `plan: free`. Without that, Render picks a paid default.

## Deploy

1. Push a branch that contains `render.yaml` to GitHub.
2. In the Render dashboard, choose New, then Blueprint, and connect the repository and branch.
3. Check that all three resources show the free plan, then apply.
4. When the deploy is live, open the service URL. `/health` should return `{"postgres":"ok","redis":"ok"}`.

Render generates `ADMIN_API_KEY`. Read it from the service's Environment page when you need administrative routes.
The [Blueprint reference](https://render.com/docs/blueprint-spec) describes every field.
Its JSON Schema is at `https://render.com/schema/render.yaml.json`. Validate edits with:

```powershell
uvx check-jsonschema --schemafile https://render.com/schema/render.yaml.json render.yaml
```

## What the Blueprint sets

| Variable | Value | Reason |
| --- | --- | --- |
| `ENVIRONMENT` | `production` | Rejects short administrator secrets, marks cookies Secure, and requires HTTPS origins |
| `DEMO_ENABLED` | `true` | Serves the page at `/` and the `/demo` routes |
| `DEMO_EMBEDDED_GATEWAYS` | `true` | Runs the mock gateways inside the API process, so no gateway service is needed |
| `ADMIN_API_KEY` | Generated | A random 256-bit value that never appears in the repository |
| `DATABASE_URL` | From the database | Private connection string |
| `REDIS_URL` | From Key Value | Private connection string |
| `DB_POOL_SIZE` | `5` | The free database allows few connections |
| `FORWARDED_ALLOW_IPS` | `*` | Lets Uvicorn read the visitor address from Render's proxy headers |

Both datastores have an empty `ipAllowList`, so only services in the same workspace can reach them.
The container listens on Render's `PORT`.

## Restarts and sleep

The embedded mocks keep their records in process memory, so a restart or spin-down loses them.
The app records its boot time and rejects demo sessions created before it.
Visitors then start a new session. Ledger rows from earlier sessions stay in Postgres.
A restart also clears Key Value, which resets rate-limit buckets and routing health.

Keep `numInstances: 1`. A second instance would have its own mock gateways and could not find the first one's payments.

## Known limits

- Uvicorn takes the leftmost `X-Forwarded-For` address when it trusts every proxy, and a visitor can forge that.
  The per-address session limit (a burst of three, then one a minute) is therefore advisory.
  `DEMO_SESSION_DAILY_LIMIT` (200 by default) is the hard bound on new demo merchants.
- The merchant and administrative API and `/docs` stay reachable.
  Merchant keys for demo sessions are never sent to the browser, and administrative routes need the generated key.
- The Blueprint has no backup or restore procedure, and the free database expires.
  Treat the hosted data as disposable.
