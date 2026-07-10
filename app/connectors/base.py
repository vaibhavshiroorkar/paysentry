import asyncio
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx

from app.cards import Card

# See docs/design/gateways.md, "The four outcomes". not_found only comes from lookups.
Outcome = Literal["approved", "declined", "not_processed", "unknown", "not_found"]


@dataclass(frozen=True)
class GatewayResult:
    outcome: Outcome
    gateway_reference: str | None = None
    status: str | None = None  # authorized, captured or voided, once approved
    decline_code: str | None = None


NOT_PROCESSED = GatewayResult("not_processed")
UNKNOWN = GatewayResult("unknown")
NOT_FOUND = GatewayResult("not_found")


class Connector(Protocol):
    name: str

    async def authorize(
        self, *, reference: str, amount_minor: int, currency: str, card: Card, capture: bool
    ) -> GatewayResult: ...

    async def lookup(self, *, reference: str) -> GatewayResult: ...

    async def capture(
        self, *, gateway_reference: str, reference: str, amount_minor: int, currency: str
    ) -> GatewayResult: ...

    async def void(self, *, gateway_reference: str, reference: str) -> GatewayResult: ...

    async def refund(
        self, *, gateway_reference: str, reference: str, amount_minor: int, currency: str
    ) -> GatewayResult: ...


class HttpConnector:
    def __init__(self, name: str, client: httpx.AsyncClient, timeout_sec: float):
        self.name = name
        self._client = client
        self._timeout = timeout_sec

    async def send(self, method: str, path: str, **kwargs) -> httpx.Response | GatewayResult:
        """One gateway call under the deadline. Returns the response, or the
        outcome when there is no response to read."""
        try:
            async with asyncio.timeout(self._timeout):
                response = await self._client.request(method, path, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            # The connection never opened, so the gateway never saw the request.
            return NOT_PROCESSED
        except (TimeoutError, httpx.TransportError):
            # The request may have arrived. Only the gateway knows what it did.
            return UNKNOWN
        if response.status_code == 503:
            # Both gateways promise that 503 means nothing was processed.
            return NOT_PROCESSED
        if response.status_code >= 500:
            return UNKNOWN
        return response


def refused(response: httpx.Response, code: str | None) -> GatewayResult:
    """A 4xx the gateway gave on purpose: it did nothing and will not."""
    return GatewayResult("declined", decline_code=code or f"gateway_{response.status_code}")
