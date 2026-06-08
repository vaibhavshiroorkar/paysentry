import uuid

import asyncpg
import pytest

from helpers import create_account, create_merchant, funded_account, transfer
from payment_helpers import captured_payment, refund


async def test_reconciliation_is_clean_after_activity(client):
    a = await funded_account(client, 1_000)
    b = await create_account(client, owner_id="user-2")
    await transfer(client, a, b, 400)
    await transfer(client, a, b, 10_000)

    report = (await client.get("/internal/reconciliation")).json()
    assert report == {"ok": True, "mismatches": []}


async def test_reconciliation_catches_a_tampered_balance(make_client, db):
    client = await make_client(check_reconciliation=False)
    a = await funded_account(client, 1_000)

    await db.execute("UPDATE accounts SET balance_minor = balance_minor + 1 WHERE id = $1", uuid.UUID(a))

    report = (await client.get("/internal/reconciliation")).json()
    assert report["ok"] is False
    kinds = {m["kind"] for m in report["mismatches"]}
    assert kinds == {"balance_mismatch", "currency_imbalance"}


async def test_ledger_entries_are_append_only(client, db):
    await funded_account(client, 1_000)
    with pytest.raises(asyncpg.RaiseError, match="append-only"):
        await db.execute("UPDATE ledger_entries SET amount_minor = 1")
    with pytest.raises(asyncpg.RaiseError, match="append-only"):
        await db.execute("DELETE FROM ledger_entries")


async def test_reconciliation_catches_payments_that_disagree_with_the_ledger(make_client, db):
    client = await make_client(check_reconciliation=False)
    _, key = await create_merchant(client)
    capture_tampered = await captured_payment(client, key, 1_000)
    refund_tampered = await captured_payment(client, key, 1_000)
    await refund(client, key, refund_tampered, 300)

    await db.execute("UPDATE payments SET amount_captured = 999 WHERE id = $1", uuid.UUID(capture_tampered))
    await db.execute("UPDATE payments SET amount_refunded = 200 WHERE id = $1", uuid.UUID(refund_tampered))

    report = (await client.get("/internal/reconciliation")).json()
    assert {m["kind"] for m in report["mismatches"]} == {"payment_capture_mismatch", "payment_refund_mismatch"}
