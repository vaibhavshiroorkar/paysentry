from typing import Literal

from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class GatewayConfig(BaseModel):
    flavour: Literal["alpha", "beta", "stripe"]
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
    environment: Literal["development", "production"] = "development"
    demo_enabled: bool = False
    demo_embedded_gateways: bool = False
    demo_session_daily_limit: int = Field(default=200, ge=1, le=10_000)
    # A header the hosting edge sets to the visitor's address and overwrites if
    # the client sent one, such as CF-Connecting-IP on Render. The demo's
    # per-address limit reads it. Empty uses the socket address.
    client_ip_header: str = ""
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # See docs/design/gateways.md, "Settings".
    gateways: dict[str, GatewayConfig] = {
        "alpha": GatewayConfig(flavour="alpha", url="http://localhost:9001"),
        "beta": GatewayConfig(flavour="beta", url="http://localhost:9002"),
    }
    # A Stripe secret key for the stripe flavour. Only test mode keys are accepted.
    stripe_secret_key: SecretStr | None = None
    gateway_timeout_sec: float = 2.0
    recovery_after_sec: float = 60.0
    recovery_interval_sec: float = 10.0

    # See docs/design/routing.md, "Settings".
    routing_strategy: Literal["success_rate", "priority", "contextual"] = "success_rate"
    routing_window: int = 100
    routing_max_attempts: int = 3
    routing_seed: int | None = None
    routing_alpha: float = 0.2
    routing_lambda: float = 1.0
    routing_decay: float = 0.998
    circuit_failure_threshold: int = 5
    circuit_cooldown_sec: float = 30.0

    # Token bucket per paying account: `capacity` requests in a burst,
    # then `refill_per_sec` requests per second after that.
    rate_limit_capacity: int = 10
    rate_limit_refill_per_sec: float = 5.0
    # If Redis is down, keep taking payments rather than reject everyone.
    rate_limit_fail_open: bool = True

    @model_validator(mode="after")
    def hosted_configuration(self):
        if self.environment == "production" and len(self.admin_api_key) < 32:
            raise ValueError("Production requires an administrator secret of at least 32 characters")
        if self.demo_embedded_gateways and not self.demo_enabled:
            raise ValueError("Embedded gateways require the demo to be enabled")
        if any(config.flavour == "stripe" for config in self.gateways.values()):
            key = self.stripe_secret_key.get_secret_value() if self.stripe_secret_key else ""
            # PaySentry is a demonstration. A live key would move real money.
            if not key.startswith(("sk_test_", "rk_test_")):
                raise ValueError("A stripe gateway needs STRIPE_SECRET_KEY set to a test mode key")
        return self
