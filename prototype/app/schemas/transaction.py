from pydantic import BaseModel, Field, ConfigDict
from decimal import Decimal
from typing import Optional
from datetime import datetime
from enum import Enum
from uuid import UUID

class TransactionStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"

class TransactionCurrency(str, Enum):
    USD = "USD"
    INR = "INR"
    EUR = "EUR"

class TransactionBase(BaseModel):
    amount: Decimal = Field(..., gt=0, le=100000, decimal_places=2)
    currency: TransactionCurrency = TransactionCurrency.USD
    user_id: UUID

class TransactionCreate(TransactionBase):
    pass 

class TransactionResponse(TransactionBase):
    id: UUID
    status: str
    risk_score: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)