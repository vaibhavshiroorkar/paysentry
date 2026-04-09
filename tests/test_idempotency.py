from helpers import balance, create_account, funded_account, new_key, transfer


async def test_retry_replays_the_first_response_and_moves_money_once(client):
    a = await funded_account(client, 1_000)
    b = await create_account(client, owner_id="user-2")
    key = new_key()

    first = await transfer(client, a, b, 300, key=key)
    retry = await transfer(client, a, b, 300, key=key)

    assert first.status_code == retry.status_code == 201
    assert retry.content == first.content
    assert "Idempotent-Replayed" not in first.headers
    assert retry.headers["Idempotent-Replayed"] == "true"
    assert await balance(client, a) == 700


async def test_same_key_with_different_body_is_rejected(client):
    a = await funded_account(client, 1_000)
    b = await create_account(client, owner_id="user-2")
    key = new_key()

    await transfer(client, a, b, 300, key=key)
    response = await transfer(client, a, b, 301, key=key)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "idempotency_key_reused"
    assert await balance(client, a) == 700


async def test_failed_outcome_is_replayed_too(client):
    a = await funded_account(client, 100)
    b = await create_account(client, owner_id="user-2")
    key = new_key()

    first = await transfer(client, a, b, 500, key=key)
    # Even after the account gains enough money, the retry gets the original answer.
    await transfer(client, await funded_account(client, 1_000), a, 1_000)
    retry = await transfer(client, a, b, 500, key=key)

    assert first.status_code == retry.status_code == 422
    assert retry.json() == first.json()
    assert retry.headers["Idempotent-Replayed"] == "true"
    assert await balance(client, a) == 1_100
