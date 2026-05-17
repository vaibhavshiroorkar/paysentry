import httpx

from helpers import merchant_headers, new_key

VISA = "4242424242424242"
DECLINED = "4000000000000002"
NO_FUNDS = "4000000000009995"
CAPTURE_FAILS = "4000000000000341"
REFUND_FAILS = "4000000000005126"
ALL_TEST_CARDS = [VISA, DECLINED, NO_FUNDS, CAPTURE_FAILS, REFUND_FAILS]


def card(number: str = VISA, exp_year: int = 2030) -> dict:
    return {"number": number, "exp_month": 12, "exp_year": exp_year, "cvc": "987"}


async def create_payment(
    client: httpx.AsyncClient,
    api_key: str,
    amount: int = 1_000,
    capture_method: str = "automatic",
    number: str = VISA,
    key: str | None = None,
) -> httpx.Response:
    return await client.post(
        "/payments",
        json={"amount_minor": amount, "currency": "INR", "capture_method": capture_method, "card": card(number)},
        headers=merchant_headers(api_key, key or new_key()),
    )


async def authorized_payment(client: httpx.AsyncClient, api_key: str, amount: int = 1_000, number: str = VISA) -> str:
    response = await create_payment(client, api_key, amount, "manual", number)
    assert response.json()["status"] == "authorized", response.text
    return response.json()["id"]


async def captured_payment(client: httpx.AsyncClient, api_key: str, amount: int = 1_000, number: str = VISA) -> str:
    response = await create_payment(client, api_key, amount, "automatic", number)
    assert response.json()["status"] == "captured", response.text
    return response.json()["id"]


async def capture(
    client: httpx.AsyncClient, api_key: str, payment_id: str, amount: int | None = None, key: str | None = None
) -> httpx.Response:
    # With no amount, send no body at all: that must capture the full amount.
    body = {} if amount is None else {"json": {"amount_minor": amount}}
    return await client.post(
        f"/payments/{payment_id}/capture", headers=merchant_headers(api_key, key or new_key()), **body
    )


async def void(client: httpx.AsyncClient, api_key: str, payment_id: str, key: str | None = None) -> httpx.Response:
    return await client.post(f"/payments/{payment_id}/void", headers=merchant_headers(api_key, key or new_key()))


async def refund(
    client: httpx.AsyncClient, api_key: str, payment_id: str, amount: int, key: str | None = None
) -> httpx.Response:
    return await client.post(
        f"/payments/{payment_id}/refunds",
        json={"amount_minor": amount},
        headers=merchant_headers(api_key, key or new_key()),
    )


async def get_payment(client: httpx.AsyncClient, api_key: str, payment_id: str) -> dict:
    response = await client.get(f"/payments/{payment_id}", headers=merchant_headers(api_key))
    assert response.status_code == 200, response.text
    return response.json()


async def merchant_balance(client: httpx.AsyncClient, api_key: str, currency: str = "INR") -> int:
    response = await client.get("/balance", headers=merchant_headers(api_key))
    return next(b["balance_minor"] for b in response.json()["balances"] if b["currency"] == currency)
