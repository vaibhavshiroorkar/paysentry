import uuid

import pytest

from app import idempotency
from app.errors import IdempotencyKeyReused, RequestInProgress
from app.idempotency import StoredResponse


async def make_merchant(db) -> uuid.UUID:
    return await db.fetchval(
        "INSERT INTO merchants (name, api_key_hash, api_key_prefix) VALUES ('m', $1, 'sk_test') RETURNING id",
        uuid.uuid4().hex,
    )


def claim(db, merchant_id, key="k1", path="/payments", body=None):
    return idempotency.claim(
        db, merchant_id=merchant_id, key=key, method="POST", path=path, fingerprint=body or {"amount": 1}
    )


async def test_new_key_is_claimed(client, db):
    merchant = await make_merchant(db)
    assert await claim(db, merchant) is None


async def test_key_still_in_progress_is_rejected(client, db):
    merchant = await make_merchant(db)
    await claim(db, merchant)

    with pytest.raises(RequestInProgress):
        await claim(db, merchant)


async def test_completed_key_replays_the_stored_response(client, db):
    merchant = await make_merchant(db)
    await claim(db, merchant)
    first = await idempotency.complete(
        db, merchant_id=merchant, key="k1", status_code=201, body={"id": "p1", "status": "captured"}
    )

    again = await claim(db, merchant)

    assert first == StoredResponse(201, {"id": "p1", "status": "captured"}, replayed=False)
    assert again == StoredResponse(201, {"id": "p1", "status": "captured"}, replayed=True)


async def test_same_key_on_a_different_request_is_rejected(client, db):
    merchant = await make_merchant(db)
    await claim(db, merchant)

    with pytest.raises(IdempotencyKeyReused):
        await claim(db, merchant, path="/payments/abc/capture")
    with pytest.raises(IdempotencyKeyReused):
        await claim(db, merchant, body={"amount": 2})


async def test_keys_are_scoped_per_merchant(client, db):
    first, second = await make_merchant(db), await make_merchant(db)
    assert await claim(db, first) is None
    assert await claim(db, second) is None
