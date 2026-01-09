from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from decimal import Decimal



class TransactionStatus(str, Enum):
    INITIATED = "initiated"
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    
@dataclass
class Transaction:
    id: str
    amount: Decimal
    currency: str 
    status: TransactionStatus
    timestamp: datetime