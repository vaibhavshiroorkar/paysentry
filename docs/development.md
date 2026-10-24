# Local development

Run commands from the repository root. The active application is outside `prototype/`.
Use Python 3.12, uv, Docker, and Docker Compose.
The root project requires Python `>=3.12,<3.13`.

## Run the whole stack

```powershell
docker compose up --build
```

Compose starts Postgres, Redis, the API, and three mock gateways.
Open `http://localhost:8000/docs` after startup.

| Service | Host port | Purpose |
| --- | --- | --- |
| API | 8000 | Merchant and administrative API |
| Postgres | 5432 | Payments and ledger |
| Redis | 6379 | Limits, gateway health and routing model |
| Gateway alpha | 9001 | Alpha HTTP contract |
| Gateway beta | 9002 | Beta HTTP contract |
| Gateway gamma | 9003 | Alpha contract, restricted to INR in Compose |

Stop containers with `docker compose stop` when you want to reuse them later.
The development Compose file does not configure named database volumes or a backup policy.
Do not rely on removing and recreating its containers to preserve data.

## Run Python locally

Start just the dependencies and mock gateways:

```powershell
docker compose up -d db redis gateway-alpha gateway-beta gateway-gamma
uv sync --frozen
uv run uvicorn app.main:app --reload
```

The default local settings use alpha and beta.
Compose's API container explicitly configures all three gateways.
Stop the Compose API before running a local API on the same port:

```powershell
docker compose stop api
```

Activation is optional when using `uv run`.
For an interactive PowerShell environment:

```powershell
.\.venv\Scripts\Activate.ps1
```

The environment prompt is `paysentry`.
Run `deactivate` before activating again if an existing terminal still shows an older prompt.

## Run the browser demo

The demo page needs only Postgres and Redis. Embedded mode runs the mock gateways inside the API:

```powershell
docker compose up -d db redis
$env:DEMO_ENABLED = 'true'
$env:DEMO_EMBEDDED_GATEWAYS = 'true'
uv run uvicorn app.main:app --reload
```

Open `http://localhost:8000/` and follow the five steps.
Each session gets its own merchant, so earlier visitors' payments stay out of view.
Without `DEMO_EMBEDDED_GATEWAYS`, the demo uses the gateways in `GATEWAYS`, as normal payments do.
The [demo design](design/demo.md) covers sessions and limits. [Hosting](hosting.md) covers the free Render deployment.

## Configuration

Settings come from [app/config.py](../app/config.py).
Local Python reads `.env` from the working directory; process environment values take precedence.
Create `.env` by copying [.env.example](../.env.example), then edit it for your local services.
Keep `.env` out of Git.

Compose interpolates selected values and explicitly passes its own container settings.
It does not automatically forward every local `.env` setting to the API container.
Container connection URLs use service names such as `db`; local Python uses `localhost`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://paysentry:paysentry@localhost:5432/paysentry` | Application database |
| `REDIS_URL` | `redis://localhost:6379/0` | Application Redis database |
| `DB_POOL_SIZE` | `20` | Maximum connections per app instance; minimum pool size is 2 |
| `REDIS_TIMEOUT_SEC` | `0.5` | Redis connection and socket timeout |
| `ADMIN_API_KEY` | `dev-admin-key` | Shared administrator credential; production requires at least 32 characters |
| `ENVIRONMENT` | `development` | `production` also marks demo cookies Secure and requires HTTPS demo origins |
| `DEMO_ENABLED` | `false` | Serve the browser demo at `/` and its `/demo` routes |
| `DEMO_EMBEDDED_GATEWAYS` | `false` | Run the mock gateways in process for the demo; requires `DEMO_ENABLED` |
| `DEMO_SESSION_DAILY_LIMIT` | `200` | New demo sessions allowed per database day, from 1 to 10000 |
| `GATEWAYS` | Alpha on 9001 and beta on 9002 | JSON map of connector names and capabilities |
| `GATEWAY_TIMEOUT_SEC` | `2.0` | Whole gateway call deadline; connect timeout is half this |
| `RECOVERY_AFTER_SEC` | `60.0` | Minimum age before recovery claims an operation |
| `RECOVERY_INTERVAL_SEC` | `10.0` | Background pass interval; nonpositive values disable the loop |
| `ROUTING_STRATEGY` | `success_rate` | `priority`, `success_rate`, or `contextual` |
| `ROUTING_WINDOW` | `100` | Recent authorization outcomes retained per gateway |
| `ROUTING_MAX_ATTEMPTS` | `3` | Maximum gateways in a new authorization plan |
| `ROUTING_SEED` | Unset | Optional local pseudorandom seed |
| `ROUTING_ALPHA` | `0.2` | Contextual model exploration weight |
| `ROUTING_LAMBDA` | `1.0` | Contextual model ridge prior |
| `ROUTING_DECAY` | `0.998` | Evidence decay per contextual routing decision |
| `CIRCUIT_FAILURE_THRESHOLD` | `5` | Consecutive technical failures before opening |
| `CIRCUIT_COOLDOWN_SEC` | `30.0` | Delay before a half-open probe |
| `RATE_LIMIT_CAPACITY` | `10` | Token bucket burst capacity |
| `RATE_LIMIT_REFILL_PER_SEC` | `5.0` | Tokens replenished each second |
| `RATE_LIMIT_FAIL_OPEN` | `true` | Allow requests when Redis rate limiting fails |

Gateway objects require `flavour` and `url`.
Flavour is `alpha` or `beta`. Optional `currencies` and `brands` restrict eligibility.
Map order supplies priority order.
For example:

```json
{
  "alpha": {"flavour":"alpha","url":"http://localhost:9001"},
  "beta": {"flavour":"beta","url":"http://localhost:9002","brands":["visa","mastercard"]},
  "gamma": {"flavour":"alpha","url":"http://localhost:9003","currencies":["INR"]}
}
```

Use positive timeout, window, rate, and capacity values.
Several numeric settings currently lack explicit range validators.
Invalid values can therefore fail at runtime rather than during settings loading.

## A payment in PowerShell

Start the stack first. These examples use the default local administrator key.

```powershell
$base = 'http://localhost:8000'
$adminHeaders = @{ 'X-Admin-Key' = 'dev-admin-key' }
$merchant = Invoke-RestMethod -Method Post -Uri "$base/merchants" -Headers $adminHeaders -ContentType 'application/json' -Body '{"name":"Acme demo"}'
$merchantHeaders = @{ Authorization = "Bearer $($merchant.api_key)" }
$createHeaders = $merchantHeaders + @{ 'Idempotency-Key' = 'demo-order-1' }
$paymentBody = @{
    amount_minor = 50000
    currency = 'INR'
    capture_method = 'manual'
    card = @{ number = '4242424242424242'; exp_month = 12; exp_year = 2030; cvc = '123' }
} | ConvertTo-Json -Depth 3
$payment = Invoke-RestMethod -Method Post -Uri "$base/payments" -Headers $createHeaders -ContentType 'application/json' -Body $paymentBody
$payment.status
```

Expected state: `authorized`. Capture INR 400.00, then refund INR 150.00:

```powershell
$captureHeaders = $merchantHeaders + @{ 'Idempotency-Key' = 'demo-capture-1' }
$captureUri = "$base/payments/$($payment.id)/capture"
Invoke-RestMethod -Method Post -Uri $captureUri -Headers $captureHeaders -ContentType 'application/json' -Body '{"amount_minor":40000}'
$replayed = Invoke-WebRequest -UseBasicParsing -Method Post -Uri $captureUri -Headers $captureHeaders -ContentType 'application/json' -Body '{"amount_minor":40000}'
$replayed.Headers['Idempotent-Replayed']
$refundHeaders = $merchantHeaders + @{ 'Idempotency-Key' = 'demo-refund-1' }
Invoke-RestMethod -Method Post -Uri "$base/payments/$($payment.id)/refunds" -Headers $refundHeaders -ContentType 'application/json' -Body '{"amount_minor":15000}'
Invoke-RestMethod -Uri "$base/balance" -Headers $merchantHeaders
Invoke-RestMethod -Uri "$base/internal/reconciliation" -Headers $adminHeaders
```

Expected replay header: `true`. Expected INR balance: `25000` minor units.
Expected reconciliation: `ok` is true and `mismatches` is empty.
Creating a fresh merchant makes the fixed example keys reusable for another walkthrough.
The [API reference](api.md) explains request states and error responses.

## Tests and routing experiments

```powershell
docker compose up -d db redis
uv run python -m pytest -q
uv run python scripts/simulate_routing.py 100
```

The simulation count should be a positive multiple of 10 because traffic runs in batches of 10.
Small runs are smoke checks; use longer repeated runs to compare routing strategies.

Tests default to `paysentry_test` and Redis database 1.
The simulation uses `paysentry_sim` and Redis database 2 with hardcoded local connection details.
They reset their dedicated state. Keep valuable application data out of those locations.
The [testing guide](testing.md) explains fixtures, focused test commands, and extension points.

## Common setup problems

| Symptom | Check |
| --- | --- |
| Port 5432, 6379, or 8000 is occupied | Check existing services before starting another copy |
| Database connection refused | Wait for `docker compose ps` to show healthy dependencies |
| Healthy container but connection still refused | Check published ports and attached networks; internal health does not prove host reachability |
| Local API cannot reach a gateway | Start the gateway services and use host ports in local settings |
| Container API cannot reach Postgres | Use `db:5432`, rather than container-local `localhost` |
| Missing column after pulling changes | Startup schema application does not alter existing tables; use a fresh disposable database or an explicit upgrade |
| Test database creation fails | Test database credentials need permission to create the dedicated database |
| `401 unauthorized` | Check administrator versus merchant headers and the active environment |
| `429 rate_limited` | Wait for refill and honor `Retry-After` |

Do not reset a database to work around a schema problem if its data matters.
The [operations guide](operations.md) covers inspection and recovery without deleting state.
