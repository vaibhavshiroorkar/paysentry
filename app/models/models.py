from decimal import Decimal
from dataclasses import dataclass
from enum import Enum
import random
import asyncio

@dataclass
class User:
    user_id: str
    name: str

class TransactionStatus(Enum):
    SUCCESS: str = "SUCCESS"
    PENDING: str = "PENDING"
    INITIATED: str = "INITIATED"
    FAILURE: str = "FAILURE"

@dataclass
class Transaction:
    txn_id: str
    payer: User
    payee: User
    amount: Decimal
    currency: str = "INR"
    status: TransactionStatus = TransactionStatus.PENDING

    def is_high_value(self) -> bool: 
        return self.amount > Decimal("10000")

@dataclass
class CardTransaction(Transaction):
    card_network: str = "UNKNOWN"

    def is_high_value(self):
        return self.amount > Decimal("50000")
    
async def stream_transactions(count: int):
    payer = User(user_id="1", name="A")
    payee = User(user_id="2", name="Z")
    for _ in range(count):
        await asyncio.sleep(0.1)
    yield Transaction("1", payer, payee, Decimal("1000"))


async def main():
    async for txn in stream_transactions:
        print("Received: {txn.txn_id}")

if __name__ == "__main__":
    asyncio.run(main())

