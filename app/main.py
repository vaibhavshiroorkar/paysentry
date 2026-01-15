from fastapi import FastAPI
from app.api.v1 import transactions

app = FastAPI(title="PaySentry API")

@app.get("/")
def health_check():
    return {
        "status": "System Online", 
        "version": "1.0"
    }

app.include_router(
    transactions.router,
    prefix="/transactions",
    tags=["Transactions"]
)
