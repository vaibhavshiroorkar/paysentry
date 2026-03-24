import asyncio

from helpers import balance, create_account, top_up, transfer


async def test_burst_past_capacity_gets_429_then_recovers(make_client):
    client = await make_client(rate_limit_capacity=3, rate_limit_refill_per_sec=5)
    account_id = await create_account(client)

    responses = [await top_up(client, account_id, 100) for _ in range(4)]

    assert [r.status_code for r in responses] == [201, 201, 201, 429]
    limited = responses[-1]
    assert limited.json()["error"]["code"] == "rate_limited"
    assert limited.headers["Retry-After"] == "1"
    assert 0 < limited.json()["error"]["retry_after_ms"] <= 200
    assert await balance(client, account_id) == 300

    await asyncio.sleep(limited.json()["error"]["retry_after_ms"] / 1000 + 0.05)
    assert (await top_up(client, account_id, 100)).status_code == 201


async def test_limit_is_per_paying_account(make_client):
    client = await make_client(rate_limit_capacity=2, rate_limit_refill_per_sec=0.1)
    a = await create_account(client)
    b = await create_account(client, owner_id="user-2")
    await top_up(client, a, 1_000)
    await top_up(client, b, 1_000)

    assert (await transfer(client, a, b, 10)).status_code == 201
    assert (await transfer(client, a, b, 10)).status_code == 429
    # b has its own bucket.
    assert (await transfer(client, b, a, 10)).status_code == 201


async def test_redis_down_fails_open(make_client):
    client = await make_client(redis_url="redis://localhost:6390/0")
    account_id = await create_account(client)
    assert (await top_up(client, account_id, 100)).status_code == 201
    assert (await client.get("/health")).status_code == 503


async def test_redis_down_fails_closed_when_configured(make_client):
    client = await make_client(redis_url="redis://localhost:6390/0", rate_limit_fail_open=False)
    account_id = await create_account(client)
    response = await top_up(client, account_id, 100)
    assert response.status_code == 503
    assert await balance(client, account_id) == 0
