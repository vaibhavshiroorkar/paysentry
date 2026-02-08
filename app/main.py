from fastapi import FastAPI
from app.core.config import settings
from app.api.v1 import transactions  

app = FastAPI(
    title="PaySentry API",
    version="1.0.0",
    description="High-performance fraud detection API"
)

@app.get("/")
def health_check():
    return {
        "status": "System Online 🟢", 
        "version": "1.0"
    }

app.include_router(
    transactions.router,
    prefix="/transactions",
    tags=["Transactions"]
)