from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app import payments
from app.auth import Merchant, current_merchant, merchant_owner
from app.idempotency import idempotency_key, to_response
from app.models import CaptureRequest, PaymentCreate

router = APIRouter(prefix="/payments", tags=["payments"])


@router.post("")
async def create_payment(
    body: PaymentCreate,
    request: Request,
    merchant: Merchant = Depends(current_merchant),
    key: str = Depends(idempotency_key),
) -> JSONResponse:
    state = request.app.state
    await state.limiter.check(merchant_owner(merchant.id))
    return to_response(await payments.create_payment(state.pool, state.processor, merchant, key, body))


@router.get("/{payment_id}")
async def get_payment(payment_id: UUID, request: Request, merchant: Merchant = Depends(current_merchant)) -> dict:
    return await payments.get_payment(request.app.state.pool, merchant, payment_id)


@router.post("/{payment_id}/capture")
async def capture_payment(
    payment_id: UUID,
    request: Request,
    body: CaptureRequest | None = None,
    merchant: Merchant = Depends(current_merchant),
    key: str = Depends(idempotency_key),
) -> JSONResponse:
    state = request.app.state
    await state.limiter.check(merchant_owner(merchant.id))
    amount = body.amount_minor if body else None
    return to_response(
        await payments.capture_payment(state.pool, state.processor, merchant, payment_id, key, amount)
    )


@router.post("/{payment_id}/void")
async def void_payment(
    payment_id: UUID,
    request: Request,
    merchant: Merchant = Depends(current_merchant),
    key: str = Depends(idempotency_key),
) -> JSONResponse:
    state = request.app.state
    await state.limiter.check(merchant_owner(merchant.id))
    return to_response(await payments.void_payment(state.pool, state.processor, merchant, payment_id, key))
