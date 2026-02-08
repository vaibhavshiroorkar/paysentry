from pydantic import BaseModel, EmailStr, ConfigDict
from uuid import UUID
from datetime import datetime  
from typing import Optional

class UserBase(BaseModel):
    email: EmailStr
    full_name: Optional[str] = None
    is_active: bool = True

class UserCreate(UserBase):
    password: str

class UserResponse(UserBase):
    id: UUID
    role: str
    created_at: datetime

    model_config = ConfigDict(from_attribute=True)