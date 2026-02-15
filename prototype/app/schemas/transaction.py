from pydantic import BaseModel, Field, ConfigDict
from decimal import Decimal
from typing import Optional
from datetime import datetime
from enum import Enum
from uuid import UUID

# 1. Enums (Good practice to keep these!)
class TransactionStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"

class TransactionCurrency(str, Enum):
    USD = "USD"
    INR = "INR"
    EUR = "EUR"

# 2. Base Schema (Shared fields)
class TransactionBase(BaseModel):
    amount: Decimal = Field(..., gt=0, le=100000, decimal_places=2)
    currency: TransactionCurrency = TransactionCurrency.USD
    user_id: UUID  # <--- CRITICAL: We need to know WHO is paying!

# 3. Input (What the user sends)
class TransactionCreate(TransactionBase):
    pass 

# 4. Output (What we send back)
class TransactionResponse(TransactionBase):
    id: UUID          # Matches DB column 'id'
    status: str       # Matches DB column 'status'
    risk_score: int   # Matches DB column 'risk_score'
    created_at: datetime

    # New Pydantic v2 Config
    model_config = ConfigDict(from_attributes=True)