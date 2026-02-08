from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from uuid import UUID
from app.db.session import AsyncSessionLocal
from app.models.transactions import Transaction
from app.schemas.transaction import TransactionCreate, TransactionResponse

router = APIRouter()

# Dependency: Get DB Session
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session

# 1. Create Transaction
@router.post("/", response_model=TransactionResponse)
async def create_transaction(
    txn_data: TransactionCreate, 
    db: AsyncSession = Depends(get_db)
):
    # Map Schema -> Database Model
    new_txn = Transaction(
        user_id=txn_data.user_id,
        amount=float(txn_data.amount), # Convert Decimal to Float for DB
        currency=txn_data.currency.value,
        status="pending",
        risk_score=0
    )
    
    db.add(new_txn)
    await db.commit()
    await db.refresh(new_txn)
    return new_txn

# 2. Get Transaction by ID
@router.get("/{txn_id}", response_model=TransactionResponse)
async def get_transaction(
    txn_id: UUID,  # <--- Changed Int to UUID
    db: AsyncSession = Depends(get_db)
):
    # Query the DB directly
    result = await db.execute(select(Transaction).where(Transaction.id == txn_id))
    txn = result.scalar_one_or_none()

    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return txn