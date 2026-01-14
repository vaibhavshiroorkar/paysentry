from fastapi import FastAPI
from typing import Optional
from pydantic import BaseModel, Field
from decimal import Decimal

class Transaction(BaseModel):
    amount: float
    description: Optional[str] = None
    currency: str = "INR" # Default value

app = FastAPI(title="PaySentry API")

@app.get("/")
def health_check():
    return {
        "status": "System Online", 
        "version": "1.0"
    }

@app.get("/transaction/{txn_id}")
def get_transaction(txn_id: int):
    return {
        "txn_id": txn_id,
        "message": "Transaction found",
        "type_of_id": str(type(txn_id))
    }

@app.get("/rate/")
def get_exchange_rate(currency: str = "INR", date: Optional[str] = None):
    return {
        "currency": currency,
        "date": date,
        "rate": 90.27 if currency == "USD" else 1.0
    }

@app.post("/transactions/")
def create_transaction(txn: Transaction):
    return {
        "message": "Transaction created successfully",
        "data_received": txn
    }