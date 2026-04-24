import hashlib
import uuid

from helpers import create_merchant, merchant_headers


async def test_creating_a_merchant_needs_the_admin_key(client):
    response = await client.post("/merchants", json={"name": "Acme"}, headers={"X-Admin-Key": "wrong"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_new_merchant_key_is_returned_once_and_stored_hashed(client, db):
    response = await client.post("/merchants", json={"name": "Acme"})

    assert response.status_code == 201
    body = response.json()
    assert body["api_key"].startswith("sk_")
    row = await db.fetchrow("SELECT * FROM merchants WHERE id = $1", uuid.UUID(body["id"]))
    assert body["api_key"] not in [str(value) for value in row.values()]
    assert row["api_key_hash"] == hashlib.sha256(body["api_key"].encode()).hexdigest()


async def test_new_merchant_starts_with_zero_balances(client):
    _, key = await create_merchant(client)

    response = await client.get("/balance", headers=merchant_headers(key))

    assert response.status_code == 200
    assert response.json() == {
        "balances": [
            {"currency": "INR", "balance_minor": 0},
            {"currency": "USD", "balance_minor": 0},
        ]
    }


async def test_merchant_routes_need_a_valid_api_key(client):
    for headers in [{}, {"Authorization": "Bearer sk_wrong"}, {"Authorization": "sk_wrong"}]:
        response = await client.get("/balance", headers=headers)
        assert response.status_code == 401, headers


async def test_merchant_key_cannot_open_admin_routes(client):
    _, key = await create_merchant(client)
    headers = {**merchant_headers(key), "X-Admin-Key": ""}

    for method, path in [
        ("GET", f"/accounts/{uuid.uuid4()}"),
        ("POST", "/transfers"),
        ("GET", "/internal/reconciliation"),
        ("POST", "/merchants"),
    ]:
        response = await client.request(method, path, headers=headers, json={})
        assert response.status_code == 401, path


async def test_health_stays_open(client):
    response = await client.get("/health", headers={"X-Admin-Key": ""})
    assert response.status_code == 200
