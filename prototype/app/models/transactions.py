from sqlalchemy import String, Integer, Float, ForeignKey, DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from datetime import datetime
import uuid
from sqlalchemy.dialects.postgresql import UUID
from app.db.base import Base

# --- ADD THIS LINE HERE ---
from app.models.user import User 
# --------------------------

class Transaction(Base):
    __tablename__ = "transactions"
    # ... rest of your code ...

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    # Who made the transaction? (Links to the 'users' table)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    status: Mapped[str] = mapped_column(String, default="pending") # pending, approved, rejected
    risk_score: Mapped[int] = mapped_column(Integer, default=0)
    
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Relationship (Allows us to say transaction.user.email)
    user = relationship("User", backref="transactions")