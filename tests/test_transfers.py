import uuid

from helpers import balance, create_account, funded_account, new_key, top_up, transfer


async def test_health(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"postgres": "ok", "redis": "ok"}


async def test_new_account_starts_at_zero(client):
    account_id = await create_account(client)
    assert await balance(client, account_id) == 0


async def test_unknown_account_is_404(client):
    response = await client.get(f"/accounts/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_owner_cannot_claim_a_reserved_prefix(client):
    for owner in ["system:me", "merchant:me"]:
        response = await client.post("/accounts", json={"owner_id": owner, "currency": "INR"})
        assert response.status_code == 422, owner


async def test_top_up_credits_account(client):
    account_id = await create_account(client)
    response = await top_up(client, account_id, 5_000)
    assert response.status_code == 201
    assert response.json()["status"] == "SUCCEEDED"
    assert await balance(client, account_id) == 5_000


async def test_transfer_moves_money_with_two_balanced_entries(client, db):
    a = await funded_account(client, 10_000)
    b = await create_account(client, owner_id="user-2")

    response = await transfer(client, a, b, 2_500)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "SUCCEEDED"
    assert await balance(client, a) == 7_500
    assert await balance(client, b) == 2_500
    entries = await db.fetch(
        "SELECT account_id, amount_minor FROM ledger_entries WHERE transfer_id = $1", uuid.UUID(body["id"])
    )
    assert sorted(e["amount_minor"] for e in entries) == [-2_500, 2_500]

    fetched = await client.get(f"/transfers/{body['id']}")
    assert fetched.json() == body

    history = (await client.get(f"/accounts/{b}/entries")).json()["entries"]
    assert [e["amount_minor"] for e in history] == [2_500]


async def test_insufficient_funds_fails_and_moves_nothing(client):
    a = await funded_account(client, 100)
    b = await create_account(client, owner_id="user-2")

    response = await transfer(client, a, b, 101)

    assert response.status_code == 422
    assert response.json()["status"] == "FAILED"
    assert response.json()["failure_reason"] == "insufficient_funds"
    assert await balance(client, a) == 100
    assert await balance(client, b) == 0


async def test_missing_idempotency_key_is_400(client):
    a = await funded_account(client, 100)
    b = await create_account(client, owner_id="user-2")
    response = await client.post(
        "/transfers",
        json={"from_account_id": a, "to_account_id": b, "amount_minor": 10, "currency": "INR"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "missing_idempotency_key"


async def test_transfer_to_self_is_rejected(client):
    a = await funded_account(client, 100)
    response = await transfer(client, a, a, 10)
    assert response.status_code == 422


async def test_amount_must_be_a_positive_integer(client):
    a = await funded_account(client, 100)
    b = await create_account(client, owner_id="user-2")
    for bad in [0, -5, 10.5, "10"]:
        response = await transfer(client, a, b, bad)
        assert response.status_code == 422, bad


async def test_currency_mismatch_is_rejected_and_key_stays_free(client):
    inr = await funded_account(client, 1_000, "INR")
    usd = await create_account(client, "USD", owner_id="user-2")
    inr_2 = await create_account(client, "INR", owner_id="user-3")
    key = new_key()

    response = await transfer(client, inr, usd, 100, key=key)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "currency_mismatch"

    # The failed request rolled back, so the same key can carry a corrected request.
    response = await transfer(client, inr, inr_2, 100, key=key)
    assert response.status_code == 201
    assert "Idempotent-Replayed" not in response.headers


async def test_unknown_account_in_transfer_is_404_and_key_stays_free(client):
    a = await funded_account(client, 1_000)
    b = await create_account(client, owner_id="user-2")
    key = new_key()

    response = await transfer(client, a, str(uuid.uuid4()), 100, key=key)
    assert response.status_code == 404

    response = await transfer(client, a, b, 100, key=key)
    assert response.status_code == 201


async def test_cannot_draw_from_system_account(client, db):
    funding = await db.fetchval("SELECT id FROM accounts WHERE owner_id = 'system:funding' AND currency = 'INR'")
    b = await create_account(client)
    response = await transfer(client, str(funding), b, 1_000)
    assert response.status_code == 403
    assert await balance(client, b) == 0
