"""Recovery of operations whose gateway outcome is unknown. See docs/design/gateways.md."""

import asyncio
import uuid

import pytest

from app.connectors import GatewayResult
from app.payments import finish_authorization
from helpers import create_merchant, new_key
from payment_helpers import (
    authorized_payment,
    capture,
    captured_payment,
    create_payment,
    get_payment,
    merchant_balance,
    refund,
    void,
)
from transports import CrashOnce


async def recover(client, older_than_sec: float = 0) -> dict:
    response = await client.post("/internal/recovery/run", params={"older_than_sec": older_than_sec})
    assert response.status_code == 200, response.text
    return response.json()


async def timed_out_client(make_client, gateways, **overrides):
    """A client whose gateway calls time out, and a function that heals the gateway."""
    client = await make_client(gateway_timeout_sec=0.1, **overrides)
    gateways["alpha"].configure(unknown_rate=1.0, hang_ms=300)
    return client, lambda: gateways["alpha"].configure(unknown_rate=0.0)


async def test_unknown_authorization_is_recovered_and_the_key_replays_it(make_client, gateways):
    client, heal = await timed_out_client(make_client, gateways)
    _, key = await create_merchant(client)
    idem = new_key()
    assert (await create_payment(client, key, 1_000, key=idem)).status_code == 202

    heal()
    counts = await recover(client)
    retry = await create_payment(client, key, 1_000, key=idem)

    assert counts["authorizations"] == 1
    assert retry.status_code == 201
    assert retry.headers["Idempotent-Replayed"] == "true"
    assert retry.json()["status"] == "captured"
    assert await merchant_balance(client, key) == 1_000
    assert len(gateways["alpha"].charges_by_reference) == 1


async def test_crash_after_the_gateway_charged_is_recovered(make_client, gateways, db):
    client = await make_client(wrap_transport=lambda inner: CrashOnce(inner, after_gateway=True))
    _, key = await create_merchant(client)
    idem = new_key()

    with pytest.raises(RuntimeError):
        await create_payment(client, key, 1_000, key=idem)
    assert await db.fetchval("SELECT status FROM payment_attempts") == "pending"

    await recover(client)

    retry = await create_payment(client, key, 1_000, key=idem)
    assert retry.json()["status"] == "captured"
    assert await merchant_balance(client, key) == 1_000


async def test_crash_before_sending_ends_as_no_record(make_client, gateways):
    client = await make_client(wrap_transport=lambda inner: CrashOnce(inner, after_gateway=False))
    _, key = await create_merchant(client)
    idem = new_key()

    with pytest.raises(RuntimeError):
        await create_payment(client, key, 1_000, key=idem)
    await recover(client)

    retry = await create_payment(client, key, 1_000, key=idem)
    assert (retry.json()["status"], retry.json()["failure_reason"]) == ("failed", "gateway_no_record")
    assert gateways["alpha"].charges_by_reference == {}
    assert await merchant_balance(client, key) == 0


async def test_unknown_capture_is_recovered(make_client, gateways):
    client = await make_client(gateway_timeout_sec=0.1)
    _, key = await create_merchant(client)
    payment_id = await authorized_payment(client, key, 1_000)
    idem = new_key()

    gateways["alpha"].configure(unknown_rate=1.0, hang_ms=300)
    assert (await capture(client, key, payment_id, key=idem)).status_code == 202
    gateways["alpha"].configure(unknown_rate=0.0)
    counts = await recover(client)
    retry = await capture(client, key, payment_id, key=idem)

    assert counts["captures"] == 1
    assert (retry.status_code, retry.json()["status"]) == (200, "captured")
    assert await merchant_balance(client, key) == 1_000


async def test_unknown_void_is_recovered(make_client, gateways):
    client = await make_client(gateway_timeout_sec=0.1)
    _, key = await create_merchant(client)
    payment_id = await authorized_payment(client, key)

    gateways["alpha"].configure(unknown_rate=1.0, hang_ms=300)
    assert (await void(client, key, payment_id)).status_code == 202
    gateways["alpha"].configure(unknown_rate=0.0)
    counts = await recover(client)

    assert counts["voids"] == 1
    assert (await get_payment(client, key, payment_id))["status"] == "voided"


async def test_unknown_refund_is_recovered(make_client, gateways):
    client = await make_client(gateway_timeout_sec=0.1)
    _, key = await create_merchant(client)
    payment_id = await captured_payment(client, key, 1_000)
    idem = new_key()

    gateways["alpha"].configure(unknown_rate=1.0, hang_ms=300)
    assert (await refund(client, key, payment_id, 400, key=idem)).status_code == 202
    gateways["alpha"].configure(unknown_rate=0.0)
    counts = await recover(client)
    retry = await refund(client, key, payment_id, 400, key=idem)

    assert counts["refunds"] == 1
    assert (retry.status_code, retry.json()["status"]) == (201, "succeeded")
    assert await merchant_balance(client, key) == 600


async def test_recovery_leaves_fresh_work_alone(make_client, gateways):
    client, heal = await timed_out_client(make_client, gateways)
    _, key = await create_merchant(client)
    payment_id = (await create_payment(client, key)).json()["id"]

    heal()
    counts = await recover(client, older_than_sec=60)

    assert counts == {"authorizations": 0, "captures": 0, "voids": 0, "refunds": 0}
    assert (await get_payment(client, key, payment_id))["status"] == "processing"


async def test_lookup_that_times_out_leaves_the_payment_in_flight(make_client, gateways, db):
    client, heal = await timed_out_client(make_client, gateways)
    _, key = await create_merchant(client)
    payment_id = (await create_payment(client, key)).json()["id"]

    counts = await recover(client)

    assert counts["authorizations"] == 0
    assert (await get_payment(client, key, payment_id))["status"] == "processing"
    assert await db.fetchval("SELECT status FROM payment_attempts") == "unknown"

    # The lease from the failed run must not block the next one forever.
    await db.execute("UPDATE payment_attempts SET lease_until = NULL")
    heal()
    await recover(client)
    assert (await get_payment(client, key, payment_id))["status"] == "captured"


async def test_concurrent_finishers_record_an_outcome_once(make_client, gateways, db):
    client, _ = await timed_out_client(make_client, gateways)
    _, key = await create_merchant(client)
    await create_payment(client, key, 1_000)
    attempt_id = await db.fetchval("SELECT id FROM payment_attempts")
    outcome = GatewayResult("approved", "ch_test", status="captured")

    results = await asyncio.gather(
        *(finish_authorization(client.app.state.pool, attempt_id, outcome) for _ in range(5))
    )

    assert sum(result is not None for result in results) == 1
    assert await merchant_balance(client, key) == 1_000


async def test_two_recovery_runs_at_once_finish_once(make_client, gateways):
    client, heal = await timed_out_client(make_client, gateways)
    _, key = await create_merchant(client)
    payment_id = (await create_payment(client, key, 1_000)).json()["id"]

    heal()
    gateways["alpha"].configure(latency_ms=50)
    first, second = await asyncio.gather(recover(client), recover(client))

    assert first["authorizations"] + second["authorizations"] == 1
    assert (await get_payment(client, key, payment_id))["status"] == "captured"
    assert await merchant_balance(client, key) == 1_000


async def test_unknown_gateway_name_is_skipped(make_client, gateways, db):
    client, heal = await timed_out_client(make_client, gateways)
    _, key = await create_merchant(client)
    payment_id = (await create_payment(client, key)).json()["id"]
    await db.execute("UPDATE payment_attempts SET gateway = 'retired'")
    await db.execute("UPDATE payments SET gateway = 'retired' WHERE id = $1", uuid.UUID(payment_id))

    counts = await recover(client)

    assert counts["authorizations"] == 0
    assert (await get_payment(client, key, payment_id))["status"] == "processing"


async def test_background_loop_recovers_on_its_own(make_client, gateways):
    client, heal = await timed_out_client(make_client, gateways, recovery_interval_sec=0.2, recovery_after_sec=0)
    _, key = await create_merchant(client)
    payment_id = (await create_payment(client, key)).json()["id"]

    heal()
    for _ in range(20):
        if (await get_payment(client, key, payment_id))["status"] == "captured":
            break
        await asyncio.sleep(0.1)

    assert (await get_payment(client, key, payment_id))["status"] == "captured"
