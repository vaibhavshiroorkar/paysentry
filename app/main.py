from fastapi import FastAPI

# 1. Initialize the App
# title: Shows up in the automatic documentation
# version: Helps track updates
app = FastAPI(
    title="PaySentry Core",
    version="0.1.0",
    description="High-performance Payment Processing API"
)

# 2. A Simple Health Check Route
# GET / -> Returns a JSON message
@app.get("/")
def root():
    return {"message": "PaySentry System Online 🟢", "status": "active"}

# 3. A Health Check endpoint (Standard practice)
@app.get("/health")
def health_check():
    return {"status": "ok"}