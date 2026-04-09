import uuid

import httpx


def new_key() -> str:
    return str(uuid.uuid4())


async def create_account(client: httpx.AsyncClient, currency: str = "INR", owner_id: str = "user-1") -> str:
    response = await client.post("/accounts", json={"owner_id": owner_id, "currency": currency})
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def top_up(client: httpx.AsyncClient, account_id: str, amount_minor: int) -> httpx.Response:
    return await client.post(
        f"/accounts/{account_id}/top-ups",
        json={"amount_minor": amount_minor},
        headers={"Idempotency-Key": new_key()},
    )


async def funded_account(client: httpx.AsyncClient, amount_minor: int, currency: str = "INR") -> str:
    account_id = await create_account(client, currency)
    response = await top_up(client, account_id, amount_minor)
    assert response.status_code == 201, response.text
    return account_id


async def transfer(
    client: httpx.AsyncClient,
    from_id: str,
    to_id: str,
    amount_minor: int,
    key: str | None = None,
    currency: str = "INR",
) -> httpx.Response:
    return await client.post(
        "/transfers",
        json={
            "from_account_id": from_id,
            "to_account_id": to_id,
            "amount_minor": amount_minor,
            "currency": currency,
        },
        headers={"Idempotency-Key": key or new_key()},
    )


async def balance(client: httpx.AsyncClient, account_id: str) -> int:
    response = await client.get(f"/accounts/{account_id}")
    assert response.status_code == 200, response.text
    return response.json()["balance_minor"]
