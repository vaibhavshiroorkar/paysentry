import logging

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import Settings
from app.errors import RateLimited, ServiceUnavailable

log = logging.getLogger(__name__)

# Token bucket. Runs as one script, so concurrent requests cannot both read
# the same token count. Uses Redis server time, so app servers with skewed
# clocks still agree. Returns {allowed (0 or 1), ms until the next token}.
TOKEN_BUCKET = """
local capacity = tonumber(ARGV[1])
local refill_per_ms = tonumber(ARGV[2]) / 1000

local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)

local state = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(state[1]) or capacity
local ts = tonumber(state[2]) or now

tokens = math.min(capacity, tokens + (now - ts) * refill_per_ms)

local allowed = 0
local retry_after_ms = 0
if tokens >= 1 then
    tokens = tokens - 1
    allowed = 1
else
    retry_after_ms = math.ceil((1 - tokens) / refill_per_ms)
end

redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
-- A full refill takes capacity / rate. After that the key holds nothing useful.
redis.call('PEXPIRE', KEYS[1], math.ceil(capacity / refill_per_ms) + 1000)
return {allowed, retry_after_ms}
"""


class RateLimiter:
    def __init__(self, redis: Redis, settings: Settings):
        self._script = redis.register_script(TOKEN_BUCKET)
        self._capacity = settings.rate_limit_capacity
        self._refill_per_sec = settings.rate_limit_refill_per_sec
        self._fail_open = settings.rate_limit_fail_open

    async def check(self, account_id) -> None:
        try:
            allowed, retry_after_ms = await self._script(
                keys=[f"ratelimit:{account_id}"], args=[self._capacity, self._refill_per_sec]
            )
        except RedisError:
            if self._fail_open:
                log.warning("Rate limiter unavailable, allowing request for %s", account_id)
                return
            raise ServiceUnavailable("Rate limiter unavailable") from None
        if not allowed:
            raise RateLimited(retry_after_ms)
