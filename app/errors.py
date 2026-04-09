import math

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class ApiError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

    def headers(self) -> dict[str, str]:
        return {}

    def body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


class NotFound(ApiError):
    status_code = 404
    code = "not_found"


class MissingIdempotencyKey(ApiError):
    status_code = 400
    code = "missing_idempotency_key"


class IdempotencyKeyReused(ApiError):
    status_code = 422
    code = "idempotency_key_reused"


class CurrencyMismatch(ApiError):
    status_code = 422
    code = "currency_mismatch"


class SystemAccountNotAllowed(ApiError):
    status_code = 403
    code = "system_account_not_allowed"


class ServiceUnavailable(ApiError):
    status_code = 503
    code = "service_unavailable"


class RateLimited(ApiError):
    status_code = 429
    code = "rate_limited"

    def __init__(self, retry_after_ms: int):
        super().__init__("Too many requests for this account")
        self.retry_after_ms = retry_after_ms

    def headers(self) -> dict[str, str]:
        # Retry-After only takes whole seconds. Round up so clients never retry early.
        return {"Retry-After": str(max(1, math.ceil(self.retry_after_ms / 1000)))}

    def body(self) -> dict:
        body = super().body()
        body["error"]["retry_after_ms"] = self.retry_after_ms
        return body


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def handle_api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.body(), headers=exc.headers())
