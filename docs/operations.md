# Operations and failure walkthroughs

These procedures target the local mock stack.
They use the default administrator key and synthetic payment data.
The [development guide](development.md) covers startup and creates the PowerShell variables used below.
Change credentials and connection addresses when operating a different environment.

## Inspect service health

```powershell
docker compose ps
docker compose logs --tail 100 api
Invoke-RestMethod -Uri 'http://localhost:8000/health'
```

The health check queries Postgres and pings Redis.
It returns 503 if either fails, even though configured Redis fallback can still allow payments.
It does not test gateway reachability or prove that the recovery task is making progress.

For a healthy container that cannot be reached from the host, inspect its published ports and networks.
An internal health check can succeed while the host connection still fails.
Check `docker compose port db 5432` and `docker compose port redis 6379`.
Avoid recreating a database container until its data and mounts are understood.

## Inspect and recover pending work

First fetch the payment using its merchant credential:

```powershell
Invoke-RestMethod -Uri "$base/payments/$($payment.id)" -Headers $merchantHeaders
```

Pending payment states are `processing`, `capture_pending`, and `void_pending`.
Refunds have their own `pending` state.
A same-key request can return 409 while recovery is unresolved.
Keep that key and payment ID. Starting a separate charge can duplicate an uncertain authorization.

Inspect aggregate pending work without changing it:

```sql
SELECT status, count(*), min(created_at) AS oldest_created
FROM payments
WHERE status IN ('processing', 'capture_pending', 'void_pending')
GROUP BY status;

SELECT count(*) AS pending_refunds, min(created_at) AS oldest_created
FROM refunds
WHERE status = 'pending';

SELECT gateway, status, count(*), min(created_at) AS oldest_created
FROM payment_attempts
WHERE status IN ('pending', 'unknown')
GROUP BY gateway, status;
```

Use a read-only SQL session against the application database.
For captures and voids, inspect `updated_at` to determine how long the pending state has lasted.
`created_at` can predate the current operation.

Run one normal recovery pass:

```powershell
Invoke-RestMethod -Method Post -Uri "$base/internal/recovery/run" -Headers $adminHeaders
```

By default, work must be at least 60 seconds old.
Claims use batches of up to 100 per operation category and 30-second leases.
A pass can return zero counts because another worker holds the lease or the gateway remains unreachable.
Allow a lease to expire before concluding that work is stranded.

Authorization recovery looks up the original attempt reference and never resends card details.
Capture, void, and refund recovery resend the original stable operation reference.
The shared finalizers commit a result once when requests or workers compete.

If logs report an unknown gateway name, restore its connector configuration before retrying recovery.
Do not rename a gateway used by unresolved records without an explicit transition plan.
If gateway state was lost, gather the gateway and payment records before deciding any correction.
The in-memory mock cannot prove a historical charge after it restarts.

## Reconcile the ledger

```powershell
$report = Invoke-RestMethod -Uri "$base/internal/reconciliation" -Headers $adminHeaders
$report | ConvertTo-Json -Depth 6
```

Success is `ok: true` with no mismatches.
HTTP 200 alone does not imply success.

| Mismatch kind | What disagrees |
| --- | --- |
| `balance_mismatch` | Cached account balance and sum of its entries |
| `unbalanced_transfer` | Transfer status, entry count, or signed entry sum |
| `currency_imbalance` | Total account balances within a currency |
| `payment_capture_mismatch` | Payment capture total and successful capture posting |
| `payment_refund_mismatch` | Payment refund total, successful refunds, or refund postings |

Save the affected IDs and inspect their payment, transfer, and entry records in one consistent snapshot.
Stop the affected money-changing workflow while investigating a confirmed mismatch.
Do not edit historical entries or replace a balance just to clear the report.
Any financial correction needs an explicit new posting and a test of the original cause.
Internal reconciliation does not confirm external settlement or the contents of a bank account.

## Demonstrate an unknown authorization

Run this only against the local mock gateways after completing the development walkthrough.
The example makes every gateway act and then delay its response beyond the API deadline.
This avoids depending on which gateway the router selects.

```powershell
$gatewayPorts = 9001, 9002, 9003
foreach ($port in $gatewayPorts) {
    Invoke-RestMethod -Method Post -Uri "http://localhost:$port/_control" -ContentType 'application/json' -Body '{"unknown_rate":1.0,"hang_ms":5000,"unavailable_rate":0.0,"decline_rate":0.0,"decline_by_brand":{},"large_amount_minor":null}'
}
$timeoutHeaders = $merchantHeaders + @{ 'Idempotency-Key' = 'demo-unknown-1' }
$pending = Invoke-WebRequest -UseBasicParsing -Method Post -Uri "$base/payments" -Headers $timeoutHeaders -ContentType 'application/json' -Body $paymentBody
$pending.StatusCode
$pendingPayment = $pending.Content | ConvertFrom-Json
```

Expected HTTP status: 202. The payment stays `processing`.
Use a fresh merchant or a new operation key when repeating the exercise.

Restore prompt gateway responses before asking recovery to look up the charge:

```powershell
foreach ($port in $gatewayPorts) {
    Invoke-RestMethod -Method Post -Uri "http://localhost:$port/_control" -ContentType 'application/json' -Body '{"unknown_rate":0.0}'
}
Invoke-RestMethod -Method Post -Uri "$base/internal/recovery/run?older_than_sec=0" -Headers $adminHeaders
Invoke-RestMethod -Uri "$base/payments/$($pendingPayment.id)" -Headers $merchantHeaders
$retry = Invoke-WebRequest -UseBasicParsing -Method Post -Uri "$base/payments" -Headers $timeoutHeaders -ContentType 'application/json' -Body $paymentBody
$retry.Headers['Idempotent-Replayed']
```

The manual payment becomes `authorized`, and the original request replays its final result.
The zero-age override is for this controlled exercise after the original request has returned.
Normal recovery waits to avoid treating an authorization as absent while the original call can still arrive.

## Demonstrate safe failover

Start the API with `ROUTING_STRATEGY=priority` so the first configured gateway is predictable.
Set alpha's `unavailable_rate` to 1.0 through `/_control`.
Create a new payment and inspect `payment_attempts` for its payment ID.
The alpha attempt fails without processing; a later eligible gateway can succeed.
Set alpha's `unavailable_rate` back to 0.0 afterward.

Repeat with `unknown_rate=1.0` on alpha instead.
The original payment must remain pending rather than moving to another gateway.
Clear the delay, then recover it using the original reference.
The [routing tests](../tests/test_routing.py) automate both cases.

## Mock control fields

POST a JSON object to a gateway's `/_control` endpoint.
Values update the current behavior; omitted fields retain their previous values.
This endpoint has no authentication and is intended for isolated development.

| Field | Initial value | Effect |
| --- | --- | --- |
| `latency_ms` | `0` | Delay before deciding whether to process |
| `decline_rate` | `0.0` | Random authorization decline probability |
| `unavailable_rate` | `0.0` | Probability of 503 without processing |
| `unknown_rate` | `0.0` | Probability of delaying a response after acting |
| `hang_ms` | `5000` | Delay used for unknown outcomes |
| `seed` | Unset | Seed for repeatable random choices |
| `decline_by_brand` | Empty map | Per-brand authorization decline probability |
| `large_amount_minor` | Null | Threshold for a large-amount decline floor |
| `large_amount_decline_rate` | `0.0` | Minimum authorization decline probability above that threshold |

Specific test cards can override the random authorization outcome or fail captures and refunds.
See the [test card table](design/payments.md#test-cards).

## Shutdown and restart

The API cancels its recovery task, closes gateway clients, closes Redis, and closes its database pool.
An interrupted request can leave a committed pending operation for the next recovery pass.
Restarting the API preserves database facts when it reconnects to the same database.
Restarting a mock gateway loses its in-memory charge and refund records.

The development stack has no backup automation, durable gateway service, deployment rollback, or merchant event delivery.
