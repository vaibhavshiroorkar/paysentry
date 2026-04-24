from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

Currency = Literal["INR", "USD"]

# Strict so that "100" and 100.0 are rejected rather than coerced. The upper
# bound (1e12 paise = 1000 crore rupees) keeps balances far from bigint overflow.
AmountMinor = Annotated[int, Field(strict=True, gt=0, le=10**12)]


class AccountCreate(BaseModel):
    owner_id: str = Field(min_length=1, max_length=64)
    currency: Currency

    @field_validator("owner_id")
    @classmethod
    def not_reserved_owner(cls, value: str) -> str:
        if value.startswith(("system:", "merchant:")):
            raise ValueError("owner_id may not start with 'system:' or 'merchant:'")
        return value


class MerchantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class TopUpCreate(BaseModel):
    amount_minor: AmountMinor


class TransferCreate(BaseModel):
    from_account_id: UUID
    to_account_id: UUID
    amount_minor: AmountMinor
    currency: Currency

    @model_validator(mode="after")
    def accounts_differ(self) -> "TransferCreate":
        if self.from_account_id == self.to_account_id:
            raise ValueError("from_account_id and to_account_id must differ")
        return self
