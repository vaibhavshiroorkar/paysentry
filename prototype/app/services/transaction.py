from datetime import datetime
import uuid
from app.schemas.transaction import TransactionCreate

class TransactionService:
    def __init__(self):
        self.db = {
            1: {
                "txn_id": "1", 
                "amount": 100, 
                "status": "COMPLETED", 
                "created_at": datetime.now(), 
                "currency": "INR",
                "type": "UPI" 
            },
            2: {
                "txn_id": "2", 
                "amount": 500, 
                "status": "PENDING", 
                "created_at": datetime.now(), 
                "currency": "USD",
                "type": "CARD"
            }
        }

    async def get_transaction(self, txn_id: int):
        return self.db.get(txn_id)
    
    async def create_transaction(self, txn_data: TransactionCreate):
        new_id = str(uuid.uuid4())
        record = {
            "type": txn_data.type,
            "txn_id": new_id,
            "status": "PENDING",
            "created_at": datetime.now(),
            "amount": txn_data.amount,
            "currency": txn_data.currency,       
            "description": txn_data.description
        }
        return record