"""httpx transports that fail the way networks and processes do."""

import httpx


class FailingTransport(httpx.AsyncBaseTransport):
    """Always fails. With `inner`, the gateway acts first."""

    def __init__(self, error: Exception, inner: httpx.AsyncBaseTransport | None = None):
        self.error = error
        self.inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.inner is not None:
            await self.inner.handle_async_request(request)
        raise self.error


class StatusTransport(httpx.AsyncBaseTransport):
    """Answers every request with one status code."""

    def __init__(self, status_code: int):
        self.status_code = status_code

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(self.status_code, json={})


class CrashOnce(httpx.AsyncBaseTransport):
    """Kills the first request, as if PaySentry died mid-call, then behaves.

    `after_gateway=True` lets the gateway act before the crash, like a process
    dying after the gateway charged but before the outcome was saved.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport, after_gateway: bool, path_suffix: str | None = None):
        self.inner = inner
        self.after_gateway = after_gateway
        # Only a request whose path ends with this crashes, so earlier steps
        # such as the authorization before a capture go through.
        self.path_suffix = path_suffix
        self.crashed = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.crashed or (self.path_suffix and not request.url.path.endswith(self.path_suffix)):
            return await self.inner.handle_async_request(request)
        self.crashed = True
        if self.after_gateway:
            await self.inner.handle_async_request(request)
        raise RuntimeError("process died")
