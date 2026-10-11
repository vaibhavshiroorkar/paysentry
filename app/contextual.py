"""LinUCB contextual routing. See docs/design/routing.md, "Contextual ranking".

Each gateway keeps ridge regression statistics from payment features to
approval. The router scores a gateway by its predicted approval for this
payment plus an exploration bonus for how little it has seen payments like it.

Rewards are centred: an approval counts +0.5 and anything else -0.5. Ridge
regression pulls an unseen gateway's prediction to 0, so centring makes "no
data" mean 50% approval. Uncentred, it would mean 0%, and a gateway that has
declined everything would look no worse than one never tried.

Redis keys:
  routing:decisions        global count of routing decisions, the decay clock
  routing:linucb:<gateway> JSON {"A": d x d, "b": d, "t": decision count at last update}
"""

import json
from dataclasses import dataclass

import numpy as np
from redis.asyncio import Redis

from app.config import Settings

BRANDS = ("visa", "mastercard", "amex")  # anything else is "other"
AMOUNT_EDGES = (50_000, 500_000, 5_000_000)  # minor units: 500, 5,000, 50,000
LARGE = 500_000  # the amount where brand-specific behaviour often changes
CURRENCIES = ("INR", "USD")
BRAND_SLOTS = len(BRANDS) + 1
DIMENSIONS = 1 + BRAND_SLOTS + len(AMOUNT_EDGES) + 1 + len(CURRENCIES) + BRAND_SLOTS

DECISIONS = "routing:decisions"

# Decay the stored sums to now, then add one observation. One script, so
# concurrent payments on any number of API instances never lose an update.
OBSERVE = """
local gamma, reward = tonumber(ARGV[1]), tonumber(ARGV[2])
local d = #ARGV - 2
local x = {}
for i = 1, d do x[i] = tonumber(ARGV[i + 2]) end
local now = tonumber(redis.call('GET', KEYS[2]) or '0')
local raw = redis.call('GET', KEYS[1])
local A, b, t = {}, {}, now
if raw then
    local stats = cjson.decode(raw)
    A, b, t = stats.A, stats.b, stats.t
else
    for i = 1, d do
        A[i] = {}
        for j = 1, d do A[i][j] = 0 end
        b[i] = 0
    end
end
local f = math.pow(gamma, math.max(0, now - t))
for i = 1, d do
    b[i] = f * b[i] + reward * x[i]
    for j = 1, d do A[i][j] = f * A[i][j] + x[i] * x[j] end
end
redis.call('SET', KEYS[1], cjson.encode({A = A, b = b, t = now}))
return 1
"""


@dataclass(frozen=True)
class Context:
    """What the router knows about a payment when it picks a gateway."""

    currency: str
    brand: str
    amount_minor: int


def features(context: Context) -> np.ndarray:
    """One-hot encoding with a bias and brand x large-amount crosses.

    A linear model adds feature effects together, so on its own it cannot say
    "this gateway is fine for visa, except large visa payments". The crosses
    give it a weight for exactly that. See the table in docs/design/routing.md.
    """
    x = np.zeros(DIMENSIONS)
    x[0] = 1.0
    brand = BRANDS.index(context.brand) if context.brand in BRANDS else len(BRANDS)
    x[1 + brand] = 1.0
    bucket = sum(context.amount_minor >= edge for edge in AMOUNT_EDGES)
    offset = 1 + BRAND_SLOTS
    x[offset + bucket] = 1.0
    offset += len(AMOUNT_EDGES) + 1
    if context.currency in CURRENCIES:
        x[offset + CURRENCIES.index(context.currency)] = 1.0
    offset += len(CURRENCIES)
    if context.amount_minor >= LARGE:
        x[offset + brand] = 1.0
    return x


def _key(gateway: str) -> str:
    return f"routing:linucb:{gateway}"


class ContextualModel:
    def __init__(self, redis: Redis, settings: Settings):
        self._redis = redis
        self._alpha = settings.routing_alpha
        self._lambda = settings.routing_lambda
        self._gamma = settings.routing_decay
        self._observe = redis.register_script(OBSERVE)

    async def scores(self, gateways: list[str], context: Context) -> dict[str, float]:
        """Upper confidence bound of each gateway's approval for this payment.

        Counts as one routing decision, which is what moves the decay clock.
        """
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.incr(DECISIONS)
            for gateway in gateways:
                pipe.get(_key(gateway))
            now, *raws = await pipe.execute()

        x = features(context)
        scores = {}
        for gateway, raw in zip(gateways, raws):
            a, b = self._decayed(raw, now)
            # One solve gives both terms: z = A^-1 x, so thetaT x = bT z by symmetry of A.
            z = np.linalg.solve(a, x)
            scores[gateway] = float(b @ z + self._alpha * np.sqrt(x @ z))
        return scores

    def _decayed(self, raw: bytes | None, now: int) -> tuple[np.ndarray, np.ndarray]:
        prior = self._lambda * np.eye(DIMENSIONS)
        if raw is None:
            return prior, np.zeros(DIMENSIONS)
        stats = json.loads(raw)
        # gamma ** huge underflows to 0.0, which leaves the prior: no NaN.
        factor = self._gamma ** max(0, now - stats["t"])
        return prior + factor * np.array(stats["A"]), factor * np.array(stats["b"])

    async def observe(self, gateway: str, context: Context, reward: int) -> None:
        """Learn from one outcome: reward 1 for an approval, 0 otherwise."""
        await self._observe(
            keys=[_key(gateway), DECISIONS],
            args=[self._gamma, reward - 0.5, *features(context).tolist()],
        )

    async def raw(self, gateway: str) -> tuple[list[list[float]], list[float], int]:
        """Stored statistics without the prior or decay. For tests and debugging."""
        stats = json.loads(await self._redis.get(_key(gateway)))
        return stats["A"], stats["b"], stats["t"]
