from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://paysentry:paysentry@localhost:5432/paysentry"
    redis_url: str = "redis://localhost:6379/0"
    db_pool_size: int = 20
    redis_timeout_sec: float = 0.5
    # Guards /merchants, /accounts, /transfers and /internal. Set a real secret outside development.
    admin_api_key: str = "dev-admin-key"
    # Simulated network latency on every fake processor call.
    fake_processor_delay_ms: int = 0

    # Token bucket per paying account: `capacity` requests in a burst,
    # then `refill_per_sec` requests per second after that.
    rate_limit_capacity: int = 10
    rate_limit_refill_per_sec: float = 5.0
    # If Redis is down, keep taking payments rather than reject everyone.
    rate_limit_fail_open: bool = True
