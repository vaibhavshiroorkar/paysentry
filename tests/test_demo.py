import asyncio
import hashlib

import pytest
from pydantic import ValidationError

from app.config import Settings


ORIGIN = {"Origin": "http://test"}


async def start_session(client):
    response = await client.post("/demo/session", headers=ORIGIN, json={})
    assert response.status_code == 201, response.text
    return response


async def create_demo_payment(client):
    response = await client.post("/demo/payments", headers=ORIGIN, json={})
    assert response.status_code == 201, response.text
    return response


async def test_demo_routes_are_disabled_by_default(client):
    assert (await client.get("/")).status_code == 404
    assert (await client.post("/demo/session", headers=ORIGIN)).status_code == 404


async def test_demo_session_is_opaque_reusable_and_never_returns_credentials(make_client):
    client = await make_client(demo_enabled=True)
    response = await start_session(client)
    token = client.cookies.get("paysentry_demo")
    assert token and len(token) >= 32
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/demo" in cookie
    assert "api_key" not in response.text and "test-admin-key" not in response.text
    row = await client.app.state.pool.fetchrow("SELECT * FROM demo_sessions")
    assert row["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in [str(value) for value in row.values()]
    assert (await client.post("/demo/session", headers=ORIGIN, json={})).status_code == 200
    assert await client.app.state.pool.fetchval("SELECT count(*) FROM merchants") == 1


async def test_demo_page_is_locked_down_without_breaking_api_docs(make_client):
    client = await make_client(demo_enabled=True)
    page = await client.get("/")
    assert page.status_code == 200
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["cache-control"] == "no-store"
    assert (await client.get("/demo-assets/demo.js")).headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" not in (await client.get("/docs")).headers


@pytest.mark.parametrize("origin",[None, "https://another.example", "http://test.attacker.example", "http://[", "https://test"])
async def test_demo_posts_require_the_same_origin(make_client, origin):
    client = await make_client(demo_enabled=True)
    headers = {} if origin is None else {"Origin": origin}
    response = await client.post("/demo/session", headers=headers, json={})
    assert response.status_code == 403
    assert await client.app.state.pool.fetchval("SELECT count(*) FROM merchants") == 0


async def test_demo_requires_a_live_session(make_client):
    client = await make_client(demo_enabled=True)
    assert (await client.get("/demo/state")).status_code == 401
    await start_session(client)
    await client.app.state.pool.execute("UPDATE demo_sessions SET expires_at = now() - interval '1 second'")
    assert (await client.get("/demo/state")).status_code == 401
    assert (await client.post("/demo/payments", headers=ORIGIN, json={})).status_code == 401


async def test_demo_daily_cap_rolls_back_without_creating_another_merchant(make_client):
    client = await make_client(demo_enabled=True, demo_session_daily_limit=1)
    await start_session(client)
    client.cookies.clear()
    assert (await client.post("/demo/session", headers=ORIGIN, json={})).status_code == 503
    assert await client.app.state.pool.fetchval("SELECT count(*) FROM merchants") == 1
    assert await client.app.state.pool.fetchval("SELECT count(*) FROM demo_sessions") == 1


async def test_concurrent_visitors_cannot_exceed_the_demo_session_cap(make_client):
    client = await make_client(demo_enabled=True, demo_session_daily_limit=1)
    responses = await asyncio.gather(*(
        client.post("/demo/session", headers=ORIGIN, json={}) for _ in range(3)
    ))
    assert sorted(response.status_code for response in responses) == [201, 503, 503]
    assert await client.app.state.pool.fetchval("SELECT count(*) FROM merchants") == 1


async def test_demo_session_creation_fails_closed_without_redis(make_client):
    client = await make_client(demo_enabled=True, redis_url="redis://127.0.0.1:1/0")
    assert (await client.post("/demo/session", headers=ORIGIN, json={})).status_code == 503
    assert await client.app.state.pool.fetchval("SELECT count(*) FROM merchants") == 0


def test_production_rejects_development_or_short_admin_secrets():
    for secret in ("dev-admin-key", "short"):
        with pytest.raises(ValidationError, match="administrator"):
            Settings(_env_file=None, environment="production", admin_api_key=secret)


async def test_production_demo_sets_a_secure_cookie(make_client):
    client = await make_client(demo_enabled=True, environment="production", admin_api_key="x" * 40)
    client.headers["X-Admin-Key"] = "x" * 40
    response = await client.post("/demo/session", headers={"Origin": "https://test"}, json={})
    assert response.status_code == 201
    assert "secure" in response.headers["set-cookie"].lower()


async def test_embedded_mock_restart_invalidates_old_sessions(make_client):
    client = await make_client(demo_enabled=True, demo_embedded_gateways=True)
    await start_session(client)
    await client.app.state.pool.execute("UPDATE demo_sessions SET created_at = now() - interval '1 day'")
    assert (await client.get("/demo/state")).status_code == 401
    assert (await client.post("/demo/session", headers=ORIGIN, json={})).status_code == 201


async def test_demo_lifecycle_replays_and_shows_balanced_real_postings(make_client):
    client = await make_client(demo_enabled=True, demo_embedded_gateways=True)
    await start_session(client)
    created = await create_demo_payment(client)
    payment_id = created.json()["result"]["id"]
    assert created.json()["result"]["status"] == "authorized"
    replay = await create_demo_payment(client)
    assert replay.json()["replayed"] is True
    assert replay.json()["result"]["id"] == payment_id
    for action in ("capture", "refund"):
        response = await client.post(f"/demo/payments/{payment_id}/{action}", headers=ORIGIN, json={})
        assert response.status_code == (200 if action == "capture" else 201), response.text
        repeated = await client.post(f"/demo/payments/{payment_id}/{action}", headers=ORIGIN, json={})
        assert repeated.json()["replayed"] is True
    snapshot = (await client.get("/demo/state")).json()
    assert snapshot["balance_minor"] == 25_000
    assert snapshot["payment"]["amount_captured"] == 40_000
    assert snapshot["payment"]["amount_refunded"] == 15_000
    assert len(snapshot["ledger"]) == 4
    assert sum(entry["amount_minor"] for entry in snapshot["ledger"]) == 0
    assert snapshot["checks"] == {"balances_match": True, "transfers_balance": True, "payments_match": True}
    assert await client.app.state.pool.fetchval("SELECT count(*) FROM payments") == 1
    assert await client.app.state.pool.fetchval("SELECT count(*) FROM transfers") == 2
    assert "4242424242424242" not in response.text


async def test_demo_cannot_read_or_capture_another_session_payment(make_client):
    client = await make_client(demo_enabled=True)
    await start_session(client)
    payment_id = (await create_demo_payment(client)).json()["result"]["id"]
    client.cookies.clear()
    await start_session(client)
    state = (await client.get("/demo/state")).json()
    assert state["payment"] is None and state["ledger"] == [] and state["balance_minor"] == 0
    response = await client.post(f"/demo/payments/{payment_id}/capture", headers=ORIGIN, json={})
    assert response.status_code == 404


async def test_demo_rejects_arbitrary_card_inputs_without_echoing_them(make_client):
    client = await make_client(demo_enabled=True)
    await start_session(client)
    response = await client.post("/demo/payments", headers=ORIGIN, json={"card": "4242424242424242"})
    assert response.status_code == 422
    assert "4242424242424242" not in response.text
    assert await client.app.state.pool.fetchval("SELECT count(*) FROM payments") == 0


@pytest.mark.parametrize("corrupt", ["balance", "payment"])
async def test_demo_accounting_checks_detect_inconsistent_state(make_client, corrupt):
    client = await make_client(demo_enabled=True, check_reconciliation=False)
    await start_session(client)
    payment_id = (await create_demo_payment(client)).json()["result"]["id"]
    await client.post(f"/demo/payments/{payment_id}/capture", headers=ORIGIN, json={})
    if corrupt == "balance":
        await client.app.state.pool.execute("UPDATE accounts SET balance_minor = balance_minor + 1 WHERE owner_id LIKE 'merchant:%'")
    else:
        await client.app.state.pool.execute("UPDATE payments SET amount_captured = 39999")
    checks = (await client.get("/demo/state")).json()["checks"]
    assert checks["balances_match" if corrupt == "balance" else "payments_match"] is False


async def test_ending_a_session_lets_the_visitor_start_over(make_client):
    client = await make_client(demo_enabled=True)
    await start_session(client)
    await create_demo_payment(client)
    ended = await client.delete("/demo/session", headers=ORIGIN)
    assert ended.status_code == 200
    assert "max-age=0" in ended.headers["set-cookie"].lower()
    client.cookies.clear()
    assert (await client.get("/demo/state")).status_code == 401
    await start_session(client)
    assert (await client.get("/demo/state")).json()["payment"] is None
    assert await client.app.state.pool.fetchval("SELECT count(*) FROM merchants") == 2


async def test_ended_session_cookie_no_longer_works(make_client):
    client = await make_client(demo_enabled=True)
    await start_session(client)
    token = client.cookies.get("paysentry_demo")
    await client.delete("/demo/session", headers=ORIGIN)
    client.cookies.set("paysentry_demo", token, path="/demo")
    assert (await client.get("/demo/state")).status_code == 401


async def test_ending_a_session_requires_the_same_origin(make_client):
    client = await make_client(demo_enabled=True)
    await start_session(client)
    assert (await client.delete("/demo/session")).status_code == 403
    assert (await client.get("/demo/state")).status_code == 200


async def new_session_status(client, headers=None) -> int:
    client.cookies.clear()
    return (await client.post("/demo/session", headers=ORIGIN | (headers or {}), json={})).status_code


async def test_one_address_cannot_open_sessions_without_limit(make_client):
    client = await make_client(demo_enabled=True)
    assert [await new_session_status(client) for _ in range(4)] == [201, 201, 201, 429]


async def test_forged_forwarded_entries_do_not_buy_new_sessions(make_client):
    client = await make_client(demo_enabled=True, trusted_proxy_hops=1)
    # The client writes everything before the entry the proxy appended.
    statuses = [
        await new_session_status(client, {"X-Forwarded-For": f"10.0.0.{n}, 203.0.113.9"}) for n in range(4)
    ]
    assert statuses == [201, 201, 201, 429]
    assert await new_session_status(client, {"X-Forwarded-For": "198.51.100.4"}) == 201


async def test_pending_payment_returns_202_with_a_snapshot(make_client, gateways):
    client = await make_client(demo_enabled=True, gateway_timeout_sec=0.1)
    await start_session(client)
    gateways["alpha"].configure(unknown_rate=1.0, hang_ms=300)
    response = await client.post("/demo/payments", headers=ORIGIN, json={})
    assert response.status_code == 202, response.text
    assert response.json()["snapshot"]["payment"]["status"] == "processing"
    assert response.json()["snapshot"]["ledger"] == []


async def test_gateway_refusal_on_capture_keeps_the_snapshot(make_client, gateways):
    client = await make_client(demo_enabled=True, use_gateways=["alpha"])
    await start_session(client)
    payment_id = (await create_demo_payment(client)).json()["result"]["id"]
    gateways["alpha"].configure(unavailable_rate=1.0)
    response = await client.post(f"/demo/payments/{payment_id}/capture", headers=ORIGIN, json={})
    body = response.json()
    assert response.status_code in (402, 503), response.text
    assert body["result"]["error"]["message"]
    assert body["snapshot"]["payment"]["id"] == payment_id
