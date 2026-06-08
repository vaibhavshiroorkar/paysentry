"""Model-based test: random operation sequences checked against a tiny model.

For each seed, a payment gets 12 random captures, voids and refunds. The model
predicts each response code and the payment's state after it. Any mismatch
names the seed and step, so it can be replayed exactly.
"""

import random
from dataclasses import dataclass

from helpers import create_merchant
from payment_helpers import capture, create_payment, get_payment, merchant_balance, refund, void

SEEDS = range(30)
STEPS = 12


@dataclass
class PaymentModel:
    amount: int
    status: str
    captured: int = 0
    refunded: int = 0

    def capture(self, amount: int | None) -> int:
        if self.status != "authorized":
            return 409
        if amount is not None and amount > self.amount:
            return 422
        self.status, self.captured = "captured", amount or self.amount
        return 200

    def void(self) -> int:
        if self.status != "authorized":
            return 409
        self.status = "voided"
        return 200

    def refund(self, amount: int) -> int:
        if self.status != "captured":
            return 409
        if amount > self.captured - self.refunded:
            return 422
        self.refunded += amount
        if self.refunded == self.captured:
            self.status = "refunded"
        return 201


async def test_random_operation_sequences_match_the_model(client):
    _, key = await create_merchant(client)
    expected_balance = 0

    for seed in SEEDS:
        rng = random.Random(seed)
        amount = rng.randint(1, 5_000)
        automatic = rng.random() < 0.5
        created = await create_payment(client, key, amount, "automatic" if automatic else "manual")
        payment_id = created.json()["id"]
        model = PaymentModel(amount, "captured" if automatic else "authorized", amount if automatic else 0)

        for step in range(STEPS):
            op = rng.choice(["capture", "void", "refund"])
            if op == "capture":
                arg = rng.choice([None, rng.randint(1, amount + 100)])
                expected, response = model.capture(arg), await capture(client, key, payment_id, arg)
            elif op == "void":
                arg = None
                expected, response = model.void(), await void(client, key, payment_id)
            else:
                arg = rng.randint(1, amount + 100)
                expected, response = model.refund(arg), await refund(client, key, payment_id, arg)

            context = f"seed={seed} step={step} op={op} arg={arg}"
            assert response.status_code == expected, f"{context}: {response.text}"
            state = await get_payment(client, key, payment_id)
            actual = (state["status"], state["amount_captured"], state["amount_refunded"])
            assert actual == (model.status, model.captured, model.refunded), context

        expected_balance += model.captured - model.refunded

    assert await merchant_balance(client, key) == expected_balance
