"""Pick gateways for a payment. See docs/design/routing.md.

State lives in Redis so every API instance shares one view of gateway health:
  routing:window:<gateway>   list of recent authorization outcomes, 1 or 0
  circuit:<gateway>          hash: failures (error streak), open_until (ms)
  circuit:<gateway>:probe    set while one half-open probe is in flight
"""

import logging
import random

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import Settings
from app.contextual import Context, ContextualModel

log = logging.getLogger(__name__)

# Record one authorization outcome: update the success window and the breaker.
RECORD = """
local success, is_error = tonumber(ARGV[1]), tonumber(ARGV[2])
redis.call('LPUSH', KEYS[1], success)
redis.call('LTRIM', KEYS[1], 0, tonumber(ARGV[3]) - 1)
if is_error == 1 then
    local failures = redis.call('HINCRBY', KEYS[2], 'failures', 1)
    -- A failed probe reopens at once. Otherwise open at the threshold.
    if redis.call('EXISTS', KEYS[3]) == 1 or failures >= tonumber(ARGV[4]) then
        local t = redis.call('TIME')
        local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
        redis.call('HSET', KEYS[2], 'open_until', now + tonumber(ARGV[5]), 'failures', 0)
        redis.call('DEL', KEYS[3])
    end
else
    -- Any answer, even a decline, shows the gateway is up.
    redis.call('DEL', KEYS[2], KEYS[3])
end
return 1
"""

# May this request call the gateway? Closed: yes. Open: no. Past the cooldown:
# yes for exactly one caller, who becomes the probe.
ACQUIRE = """
local open_until = tonumber(redis.call('HGET', KEYS[1], 'open_until'))
if not open_until then
    return 1
end
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
if now < open_until then
    return 0
end
if redis.call('SET', KEYS[2], '1', 'NX', 'PX', tonumber(ARGV[1])) then
    return 1
end
return 0
"""

ERRORS = ("not_processed", "unknown")


def _window(gateway: str) -> str:
    return f"routing:window:{gateway}"


def _circuit(gateway: str) -> str:
    return f"circuit:{gateway}"


def _probe(gateway: str) -> str:
    return f"circuit:{gateway}:probe"


class Router:
    def __init__(self, redis: Redis, settings: Settings, rng: random.Random | None = None):
        self._redis = redis
        self._settings = settings
        self._rng = rng or random.Random(settings.routing_seed)
        self._record = redis.register_script(RECORD)
        self._acquire = redis.register_script(ACQUIRE)
        self.model = ContextualModel(redis, settings)

    def _eligible(self, currency: str, brand: str) -> list[str]:
        return [name for name, config in self._settings.gateways.items() if config.accepts(currency, brand)]

    async def plan(self, currency: str, brand: str, amount_minor: int = 0) -> list[str]:
        """Gateways to try for this payment, best first. Open circuits are left out."""
        eligible = self._eligible(currency, brand)
        try:
            seconds, micros = await self._redis.time()
            now_ms = seconds * 1000 + micros // 1000
            async with self._redis.pipeline(transaction=False) as pipe:
                for gateway in eligible:
                    pipe.hget(_circuit(gateway), "open_until")
                    pipe.lrange(_window(gateway), 0, -1)
                replies = await pipe.execute()
        except RedisError:
            # Without Redis there is no health data. Keep payments flowing in priority order.
            log.warning("Routing state unavailable, using priority order")
            return eligible

        candidates = []
        for i, gateway in enumerate(eligible):
            open_until, window = replies[2 * i], replies[2 * i + 1]
            if open_until is not None and now_ms < int(open_until):
                continue
            candidates.append((gateway, window))

        if self._settings.routing_strategy == "priority":
            return [gateway for gateway, _ in candidates]
        if self._settings.routing_strategy == "contextual":
            names = [gateway for gateway, _ in candidates]
            try:
                scores = await self.model.scores(names, Context(currency, brand, amount_minor))
            except RedisError:
                log.warning("Contextual model unavailable, using priority order")
                return names
            # sorted() is stable, so equal scores (an empty model) keep priority order.
            return sorted(names, key=scores.get, reverse=True)
        # Thompson sampling: one draw per gateway from Beta(successes + 1, failures + 1).
        samples = {}
        for gateway, window in candidates:
            successes = sum(1 for outcome in window if outcome == b"1")
            samples[gateway] = self._rng.betavariate(successes + 1, len(window) - successes + 1)
        return sorted(samples, key=samples.get, reverse=True)

    async def acquire(self, gateway: str) -> bool:
        """Call just before sending. False means skip this gateway."""
        # A probe that never reports back frees its slot after this long.
        probe_ttl_ms = int(self._settings.gateway_timeout_sec * 2_000)
        try:
            return bool(await self._acquire(keys=[_circuit(gateway), _probe(gateway)], args=[probe_ttl_ms]))
        except RedisError:
            return True

    async def record(self, gateway: str, outcome: str, context: Context | None = None) -> None:
        """Report an authorization outcome (approved, declined, not_processed, unknown).

        With a context, the contextual model learns from it as well.
        """
        try:
            await self._record(
                keys=[_window(gateway), _circuit(gateway), _probe(gateway)],
                args=[
                    1 if outcome == "approved" else 0,
                    1 if outcome in ERRORS else 0,
                    self._settings.routing_window,
                    self._settings.circuit_failure_threshold,
                    int(self._settings.circuit_cooldown_sec * 1000),
                ],
            )
        except RedisError:
            log.warning("Could not record %s for gateway %s", outcome, gateway)
            return
        if context is not None and self._settings.routing_strategy == "contextual":
            try:
                await self.model.observe(gateway, context, 1 if outcome == "approved" else 0)
            except RedisError:
                log.warning("Could not update the contextual model for gateway %s", gateway)
