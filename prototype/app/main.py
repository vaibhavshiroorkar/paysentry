from fastapi import FastAPI
from app.core.config import settings
from app.api.v1 import transactions  

app = FastAPI(
    title="PaySentry API",
    version="1.0.0",
    description="Transactions API for a fraud detection prototype"
)

@app.get("/")
def health_check():
    return {
        "status": "ok",
        "version": "1.0"
    }

app.include_router(
    transactions.router,
    prefix="/transactions",
    tags=["Transactions"]
)