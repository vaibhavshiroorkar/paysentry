"""Settle operations whose gateway outcome is unknown. See docs/design/gateways.md.

Each pass claims stuck rows under a short lease and commits, then asks the
gateway what happened with no transaction open, then finishes the operation
through the same finish_* functions the request path uses.
"""

import asyncio
import logging

import asyncpg

from app import payments
from app.connectors import Connector

log = logging.getLogger(__name__)

# Long enough for a gateway call and a finish, short enough that a crashed
# worker's rows come back soon.
LEASE = "30 seconds"
BATCH = 100

# Authorizations still waiting for an answer. Attempts are judged by when they
# were sent, so a request still inside its own deadline is never touched.
CLAIM_AUTHORIZATIONS = f"""
UPDATE payment_attempts SET lease_until = now() + interval '{LEASE}'
WHERE id IN (
    SELECT a.id FROM payment_attempts a
    JOIN payments p ON p.id = a.payment_id
    WHERE a.status IN ('pending', 'unknown') AND p.status = 'processing'
      AND a.created_at < now() - make_interval(secs => $1)
      AND (a.lease_until IS NULL OR a.lease_until < now())
    ORDER BY a.created_at
    LIMIT {BATCH}
    FOR NO KEY UPDATE OF a SKIP LOCKED
)
RETURNING id, gateway
"""

# Captures and voids in flight. updated_at is when the payment entered the state.
CLAIM_PAYMENTS = f"""
UPDATE payments SET lease_until = now() + interval '{LEASE}'
WHERE id IN (
    SELECT id FROM payments
    WHERE status = $2
      AND updated_at < now() - make_interval(secs => $1)
      AND (lease_until IS NULL OR lease_until < now())
    ORDER BY updated_at
    LIMIT {BATCH}
    FOR NO KEY UPDATE SKIP LOCKED
)
RETURNING *
"""

CLAIM_REFUNDS = f"""
UPDATE refunds SET lease_until = now() + interval '{LEASE}'
WHERE id IN (
    SELECT id FROM refunds
    WHERE status = 'pending'
      AND created_at < now() - make_interval(secs => $1)
      AND (lease_until IS NULL OR lease_until < now())
    ORDER BY created_at
    LIMIT {BATCH}
    FOR NO KEY UPDATE SKIP LOCKED
)
RETURNING *
"""


def _connector(gateways: dict[str, Connector], name: str, what: str) -> Connector | None:
    connector = gateways.get(name)
    if connector is None:
        # The gateway was removed from config. Leave the row for a person to settle.
        log.warning("Cannot recover %s: no gateway named %r is configured", what, name)
    return connector


async def run_recovery(pool: asyncpg.Pool, gateways: dict[str, Connector], older_than_sec: float) -> dict[str, int]:
    """One recovery pass. Returns how many operations of each kind it settled."""
    counts = {"authorizations": 0, "captures": 0, "voids": 0, "refunds": 0}

    for attempt in await pool.fetch(CLAIM_AUTHORIZATIONS, older_than_sec):
        connector = _connector(gateways, attempt["gateway"], f"attempt {attempt['id']}")
        if connector is None:
            continue
        # Look up, never re-send: re-sending would need the card number.
        result = await connector.lookup(reference=str(attempt["id"]))
        if result.outcome in ("unknown", "not_processed"):
            continue  # The lookup itself failed. Try again next pass.
        if await payments.finish_authorization(pool, attempt["id"], result):
            counts["authorizations"] += 1

    for status, send, finish, kind in [
        ("capture_pending", payments.send_capture, payments.finish_capture, "captures"),
        ("void_pending", payments.send_void, payments.finish_void, "voids"),
    ]:
        for payment in await pool.fetch(CLAIM_PAYMENTS, older_than_sec, status):
            connector = _connector(gateways, payment["gateway"], f"payment {payment['id']}")
            if connector is None:
                continue
            # Same reference as the original call, so the gateway acts at most once.
            result = await send(connector, payment)
            if result.outcome == "unknown":
                continue
            if await finish(pool, payment["id"], result):
                counts[kind] += 1

    for refund in await pool.fetch(CLAIM_REFUNDS, older_than_sec):
        payment = await pool.fetchrow("SELECT * FROM payments WHERE id = $1", refund["payment_id"])
        connector = _connector(gateways, payment["gateway"], f"refund {refund['id']}")
        if connector is None:
            continue
        result = await payments.send_refund(connector, payment, refund)
        if result.outcome == "unknown":
            continue
        if await payments.finish_refund(pool, refund["id"], result):
            counts["refunds"] += 1

    return counts


async def recovery_loop(pool: asyncpg.Pool, gateways: dict[str, Connector], interval_sec: float, older_than_sec: float):
    while True:
        await asyncio.sleep(interval_sec)
        try:
            counts = await run_recovery(pool, gateways, older_than_sec)
            if any(counts.values()):
                log.info("Recovery settled %s", counts)
        except Exception:
            # A bad pass must not end the loop. The rows are still there next time.
            log.exception("Recovery pass failed")
