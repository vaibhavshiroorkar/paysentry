from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from uuid import UUID
from app.db.session import AsyncSessionLocal
from app.models.transactions import Transaction
from app.schemas.transaction import TransactionCreate, TransactionResponse

router = APIRouter()

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session

@router.post("/", response_model=TransactionResponse)
async def create_transaction(
    txn_data: TransactionCreate, 
    db: AsyncSession = Depends(get_db)
):
    new_txn = Transaction(
        user_id=txn_data.user_id,
        amount=float(txn_data.amount),  # the column is Float, so this loses precision
        currency=txn_data.currency.value,
        status="pending",
        risk_score=0
    )
    
    db.add(new_txn)
    await db.commit()
    await db.refresh(new_txn)
    return new_txn

@router.get("/{txn_id}", response_model=TransactionResponse)
async def get_transaction(
    txn_id: UUID,
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(Transaction).where(Transaction.id == txn_id))
    txn = result.scalar_one_or_none()

    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return txn