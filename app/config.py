from typing import Literal

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class GatewayConfig(BaseModel):
    flavour: Literal["alpha", "beta"]
    url: str
    # What the gateway accepts. None means everything.
    currencies: list[str] | None = None
    brands: list[str] | None = None

    def accepts(self, currency: str, brand: str) -> bool:
        return (self.currencies is None or currency in self.currencies) and (
            self.brands is None or brand in self.brands
        )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://paysentry:paysentry@localhost:5432/paysentry"
    redis_url: str = "redis://localhost:6379/0"
    db_pool_size: int = 20
    redis_timeout_sec: float = 0.5
    # Guards /merchants, /accounts, /transfers and /internal. Set a real secret outside development.
    admin_api_key: str = "dev-admin-key"

    # See docs/design/gateways.md, "Settings".
    gateways: dict[str, GatewayConfig] = {
        "alpha": GatewayConfig(flavour="alpha", url="http://localhost:9001"),
        "beta": GatewayConfig(flavour="beta", url="http://localhost:9002"),
    }
    gateway_timeout_sec: float = 2.0
    recovery_after_sec: float = 60.0
    recovery_interval_sec: float = 10.0

    # See docs/design/routing.md, "Settings".
    routing_strategy: Literal["success_rate", "priority", "contextual"] = "success_rate"
    routing_window: int = 100
    routing_max_attempts: int = 3
    routing_seed: int | None = None
    routing_alpha: float = 0.5
    routing_lambda: float = 1.0
    routing_decay: float = 0.995
    circuit_failure_threshold: int = 5
    circuit_cooldown_sec: float = 30.0

    # Token bucket per paying account: `capacity` requests in a burst,
    # then `refill_per_sec` requests per second after that.
    rate_limit_capacity: int = 10
    rate_limit_refill_per_sec: float = 5.0
    # If Redis is down, keep taking payments rather than reject everyone.
    rate_limit_fail_open: bool = True
