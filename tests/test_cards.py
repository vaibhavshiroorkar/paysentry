import datetime as dt
import time
import uuid

import pytest
from pydantic import ValidationError

from app.cards import Card, card_brand, luhn_valid
from app.processor import FakeProcessor

TEST_CARDS = [
    "4242424242424242",
    "4000000000000002",
    "4000000000009995",
    "4000000000000341",
    "4000000000005126",
]


def card(number: str = "4242424242424242", **overrides) -> Card:
    return Card(**{"number": number, "exp_month": 12, "exp_year": 2030, "cvc": "987", **overrides})


def test_all_test_cards_pass_luhn():
    assert all(luhn_valid(number) for number in TEST_CARDS)


def test_luhn_catches_a_one_digit_typo():
    assert not luhn_valid("4242424242424241")


@pytest.mark.parametrize(
    "number, brand",
    [
        ("4242424242424242", "visa"),
        ("5555555555554444", "mastercard"),
        ("2223003122003222", "mastercard"),
        ("378282246310005", "amex"),
        ("6011111111111117", "unknown"),
    ],
)
def test_brand_comes_from_the_number_prefix(number, brand):
    assert card_brand(number) == brand


@pytest.mark.parametrize("number", ["4242424242424241", "4242-4242-4242-4242", "4242", "abcdabcdabcdabcd"])
def test_invalid_numbers_are_rejected(number):
    with pytest.raises(ValidationError):
        card(number)


def test_expired_card_is_rejected():
    today = dt.date.today()
    year, month = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    with pytest.raises(ValidationError):
        card(exp_month=month, exp_year=year)


def test_card_expiring_this_month_is_still_valid():
    today = dt.date.today()
    assert card(exp_month=today.month, exp_year=today.year).last4 == "4242"


@pytest.mark.parametrize("cvc", ["12", "12345", "12a"])
def test_cvc_must_be_three_or_four_digits(cvc):
    with pytest.raises(ValidationError):
        card(cvc=cvc)


def test_card_never_prints_its_number_or_cvc():
    c = card()
    text = repr(c) + str(c) + str(c.model_dump()) + c.model_dump_json()
    assert "4242424242424242" not in text
    assert "987" not in text


def test_fingerprint_keeps_only_brand_last4_and_expiry():
    assert card().fingerprint() == {"brand": "visa", "last4": "4242", "exp_month": 12, "exp_year": 2030}


async def authorize(processor: FakeProcessor, number: str, capture: bool = False):
    return await processor.authorize(
        payment_id=uuid.uuid4(), amount_minor=1_000, currency="INR", card=card(number), capture=capture
    )


async def test_processor_approves_an_ordinary_card():
    result = await authorize(FakeProcessor(), "4242424242424242")
    assert result.approved
    assert result.reference.startswith("fake_ok_")


@pytest.mark.parametrize(
    "number, code", [("4000000000000002", "card_declined"), ("4000000000009995", "insufficient_funds")]
)
async def test_processor_declines_decline_cards(number, code):
    result = await authorize(FakeProcessor(), number)
    assert not result.approved
    assert result.decline_code == code
    assert result.reference is None


async def test_capture_and_void_fail_for_the_capture_failure_card():
    processor = FakeProcessor()
    auth = await authorize(processor, "4000000000000341")
    assert auth.approved

    capture = await processor.capture(reference=auth.reference, amount_minor=1_000)
    void = await processor.void(reference=auth.reference)

    assert (capture.approved, capture.decline_code) == (False, "capture_declined")
    assert (void.approved, void.decline_code) == (False, "void_declined")


async def test_refunds_fail_for_the_refund_failure_card():
    processor = FakeProcessor()
    auth = await authorize(processor, "4000000000005126", capture=True)

    refund = await processor.refund(reference=auth.reference, amount_minor=500)

    assert auth.approved
    assert (refund.approved, refund.decline_code) == (False, "refund_declined")


async def test_ordinary_card_captures_voids_and_refunds():
    processor = FakeProcessor()
    auth = await authorize(processor, "4242424242424242")
    for result in [
        await processor.capture(reference=auth.reference, amount_minor=1_000),
        await processor.void(reference=auth.reference),
        await processor.refund(reference=auth.reference, amount_minor=1_000),
    ]:
        assert result.approved


async def test_processor_delay_simulates_latency():
    start = time.perf_counter()
    await authorize(FakeProcessor(delay_ms=50), "4242424242424242")
    assert time.perf_counter() - start >= 0.05
