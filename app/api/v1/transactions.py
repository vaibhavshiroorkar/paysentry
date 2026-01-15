from fastapi import APIRouter, HTTPException, Depends
from typing import Optional
from datetime import datetime
import uuid

from app.schemas.transaction import TransactionCreate, TransactionResponse
from app.services.transaction import TransactionService

router = APIRouter()

def get_transaction_service():
    return TransactionService()

@router.get("/{txn_id}", response_model=TransactionResponse)
async def get_transaction(
        txn_id: int,
        chef: TransactionService = Depends(get_transaction_service)
    ):
    data = await chef.get_transaction(txn_id)

    if not data:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return data


@router.post("/", response_model=TransactionResponse)
async def create_transaction(
    txn: TransactionCreate,
    service: TransactionService = Depends(get_transaction_service)
):

    return await service.create_transaction(txn)