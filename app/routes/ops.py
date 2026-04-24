from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError

from app.auth import require_admin
from app.reconcile import reconcile

router = APIRouter(tags=["ops"])


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    state = request.app.state
    checks = {}
    try:
        await state.pool.fetchval("SELECT 1")
        checks["postgres"] = "ok"
    except Exception:
        checks["postgres"] = "down"
    try:
        await state.redis.ping()
        checks["redis"] = "ok"
    except RedisError:
        checks["redis"] = "down"
    healthy = all(value == "ok" for value in checks.values())
    return JSONResponse(status_code=200 if healthy else 503, content=checks)


@router.get("/internal/reconciliation", dependencies=[Depends(require_admin)])
async def reconciliation(request: Request) -> dict:
    mismatches = await reconcile(request.app.state.pool)
    return {"ok": not mismatches, "mismatches": mismatches}
