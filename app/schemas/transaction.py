from pydantic import BaseModel, Field
from decimal import Decimal
from typing import Optional
from datetime import datetime
from enum import Enum

class TransactionStatus(str, Enum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

class TransactionType(str, Enum):
    UPI = "UPI"
    CARD = "CARD"
    NETBANKING = "NETBANKING"

class TransactionCreate(BaseModel):
    type: TransactionType = TransactionType.UPI
    amount: Decimal = Field(..., gt=0, le=100000,decimal_places=2)
    currency: str = "INR"
    description: Optional[str] = None

class TransactionResponse(BaseModel):
    type: TransactionType
    txn_id: str
    status: TransactionStatus
    amount: Decimal
    currency: str
    created_at: datetime

    class Config:
        from_attributes = True