from pydantic import BaseModel, Field
from decimal import Decimal
from typing import Optional
from datetime import datetime

class TransactionCreate(BaseModel):
    amount: Decimal = Field(..., gt=0, decimal_places=2)
    description: Optional[str] = None
    currency: str = "INR"

class TransactionResponse(BaseModel):
    txn_id: str
    status: str
    amount: Decimal
    currency: str
    created_at: datetime

    class Config:
        from_attributes = True