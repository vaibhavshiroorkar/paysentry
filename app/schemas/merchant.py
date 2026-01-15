from pydantic import BaseModel, EmailStr, Field
from typing import Optional
from datetime import datetime

class MerchantCreate(BaseModel):
    name: str = Field(..., min_length=3, max_length=100)
    email: EmailStr
    category: str = "RETAIL" # e.g., Retail, Food, Travel

class MerchantResponse(BaseModel):
    merchant_id: str
    name: str
    email: EmailStr
    is_verified: bool  # KYC Status
    created_at: datetime

    class Config:
        from_attributes = True