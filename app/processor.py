import asyncio
from dataclasses import dataclass
from uuid import UUID

from app.cards import Card


@dataclass(frozen=True)
class ProcessorResult:
    approved: bool
    reference: str | None = None
    decline_code: str | None = None


# Test cards, following Stripe's numbers where Stripe has one.
DECLINES = {
    "4000000000000002": "card_declined",
    "4000000000009995": "insufficient_funds",
}
BEHAVIOURS = {
    "4000000000000341": "capture_fails",
    "4000000000005126": "refund_fails",
}


def _behaviour(reference: str) -> str:
    return reference.removeprefix("fake_").rsplit("_", 1)[0]


class FakeProcessor:
    """Stands in for a card gateway until Phase 2 adds real connectors.

    Later calls only receive the reference from authorize, so the reference
    carries the test card's behaviour: fake_<behaviour>_<payment id>.
    """

    def __init__(self, delay_ms: int = 0):
        self._delay = delay_ms / 1000

    async def _network(self) -> None:
        if self._delay:
            await asyncio.sleep(self._delay)

    async def authorize(
        self, *, payment_id: UUID, amount_minor: int, currency: str, card: Card, capture: bool
    ) -> ProcessorResult:
        await self._network()
        number = card.number.get_secret_value()
        if number in DECLINES:
            return ProcessorResult(approved=False, decline_code=DECLINES[number])
        return ProcessorResult(approved=True, reference=f"fake_{BEHAVIOURS.get(number, 'ok')}_{payment_id.hex}")

    async def capture(self, *, reference: str, amount_minor: int) -> ProcessorResult:
        await self._network()
        if _behaviour(reference) == "capture_fails":
            return ProcessorResult(approved=False, decline_code="capture_declined")
        return ProcessorResult(approved=True, reference=reference)

    async def void(self, *, reference: str) -> ProcessorResult:
        await self._network()
        if _behaviour(reference) == "capture_fails":
            return ProcessorResult(approved=False, decline_code="void_declined")
        return ProcessorResult(approved=True, reference=reference)

    async def refund(self, *, reference: str, amount_minor: int) -> ProcessorResult:
        await self._network()
        if _behaviour(reference) == "refund_fails":
            return ProcessorResult(approved=False, decline_code="refund_declined")
        return ProcessorResult(approved=True, reference=reference)
