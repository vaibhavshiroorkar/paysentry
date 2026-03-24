"""These tests fire requests at the same moment against real Postgres.

The pool holds 20 connections, so up to 20 transactions truly overlap.
"""

import asyncio
from collections import Counter

from helpers import balance, create_account, funded_account, new_key, transfer


async def test_concurrent_duplicates_charge_exactly_once(client, db):
    a = await funded_account(client, 10_000)
    b = await create_account(client, owner_id="user-2")
    key = new_key()

    responses = await asyncio.gather(*(transfer(client, a, b, 1_000, key=key) for _ in range(50)))

    assert {r.status_code for r in responses} == {201}
    assert len({r.json()["id"] for r in responses}) == 1
    assert sum(r.headers.get("Idempotent-Replayed") == "true" for r in responses) == 49
    assert await db.fetchval("SELECT count(*) FROM transfers WHERE idempotency_key = $1", key) == 1
    assert await balance(client, a) == 9_000
    assert await balance(client, b) == 1_000


async def test_concurrent_spends_never_overdraw(client):
    a = await funded_account(client, 100)
    b = await create_account(client, owner_id="user-2")

    responses = await asyncio.gather(*(transfer(client, a, b, 30) for _ in range(50)))

    # Without row locks, many of these would read balance 100 and all succeed.
    codes = Counter(r.status_code for r in responses)
    assert codes == {201: 3, 422: 47}
    assert await balance(client, a) == 10
    assert await balance(client, b) == 90


async def test_opposite_transfers_do_not_deadlock(client):
    a = await funded_account(client, 10_000)
    b = await funded_account(client, 10_000)

    # Half go A->B and half go B->A, all at once. Locking in a fixed order means
    # no pair of transactions can each hold the row the other needs.
    responses = await asyncio.gather(
        *(transfer(client, a, b, 7) if i % 2 == 0 else transfer(client, b, a, 7) for i in range(100))
    )

    assert {r.status_code for r in responses} == {201}, [r.text for r in responses if r.status_code != 201]
    assert await balance(client, a) == 10_000
    assert await balance(client, b) == 10_000
