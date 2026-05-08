import datetime as dt

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator


def luhn_valid(number: str) -> bool:
    """The check digit scheme every card number uses. Catches most typos."""
    if not number.isdigit():
        return False
    total = 0
    for position, char in enumerate(reversed(number)):
        digit = int(char)
        if position % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def card_brand(number: str) -> str:
    if number.startswith("4"):
        return "visa"
    if number[:2] in ("34", "37"):
        return "amex"
    if 51 <= int(number[:2]) <= 55 or 2221 <= int(number[:4]) <= 2720:
        return "mastercard"
    return "unknown"


class Card(BaseModel):
    # SecretStr keeps the number and CVC out of repr, str and model_dump.
    number: SecretStr
    exp_month: int = Field(strict=True, ge=1, le=12)
    exp_year: int = Field(strict=True, ge=2000, le=2100)
    cvc: SecretStr

    @field_validator("number")
    @classmethod
    def valid_number(cls, value: SecretStr) -> SecretStr:
        number = value.get_secret_value()
        if not (number.isdigit() and 12 <= len(number) <= 19 and luhn_valid(number)):
            raise ValueError("card number is not valid")
        return value

    @field_validator("cvc")
    @classmethod
    def valid_cvc(cls, value: SecretStr) -> SecretStr:
        cvc = value.get_secret_value()
        if not (cvc.isdigit() and len(cvc) in (3, 4)):
            raise ValueError("cvc must be 3 or 4 digits")
        return value

    @model_validator(mode="after")
    def not_expired(self) -> "Card":
        today = dt.date.today()
        # A card is valid through the last day of its expiry month.
        if (self.exp_year, self.exp_month) < (today.year, today.month):
            raise ValueError("card has expired")
        return self

    @property
    def brand(self) -> str:
        return card_brand(self.number.get_secret_value())

    @property
    def last4(self) -> str:
        return self.number.get_secret_value()[-4:]

    def fingerprint(self) -> dict:
        """What may be stored or hashed. Never the full number or the CVC."""
        return {"brand": self.brand, "last4": self.last4, "exp_month": self.exp_month, "exp_year": self.exp_year}
