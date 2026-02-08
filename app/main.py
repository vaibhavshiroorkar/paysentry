from fastapi import FastAPI
from app.core.config import settings  # <--- FIXED: Correct location
# from app.api.v1 import transactions # <--- COMMENTED OUT (We don't have this file yet!)

app = FastAPI(
    title="PaySentry API",
    version="1.0.0",
    description="High-performance fraud detection API"
)

# Single Source of Truth for the "/" path
@app.get("/")
def health_check():
    return {
        "status": "System Online 🟢", 
        "version": "1.0",
        "database": settings.DATABASE_URL  # Temporary debug line
    }

# We will uncomment this LATER when we actually create the transactions file
# app.include_router(
#     transactions.router,
#     prefix="/transactions",
#     tags=["Transactions"]
# )