from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEV_JWT_SECRET = "dev-only-secret-change-me-0123456789"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: Literal["dev", "test", "prod"] = "dev"

    database_url: str = "postgresql+asyncpg://chatforge:chatforge@localhost:5432/chatforge"
    redis_url: str = "redis://localhost:6379/0"
    cors_origins: list[str] = ["http://localhost:5173"]

    jwt_secret: SecretStr = SecretStr(_DEV_JWT_SECRET)
    user_token_ttl_minutes: int = 60 * 24
    guest_token_ttl_minutes: int = 60 * 24 * 30

    # LLM
    llm_provider: Literal["fake", "anthropic"] = "fake"
    anthropic_model: str = "claude-opus-5-5"
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    llm_max_tokens: int = 16000
    history_limit: int = 20
    system_prompt: str = "You are a concise, friendly assistant. Answer in the user's language."

    # Quotas — tokens per calendar day in `quota_timezone`
    quota_timezone: str = "Asia/Seoul"
    guest_daily_tokens: int = 20_000
    free_daily_tokens: int = 200_000
    pro_daily_tokens: int = 2_000_000
    requests_per_minute: int = 20
    guest_creations_per_hour_per_ip: int = 10

    # Async jobs
    task_backend: Literal["inline", "cloudtasks"] = "inline"
    gcp_project: str = ""
    gcp_location: str = "asia-northeast3"
    tasks_queue: str = "chatforge-default"
    tasks_service_url: str = ""  # public base URL of this service, used as Cloud Tasks target
    tasks_invoker_sa: str = ""  # service account Cloud Tasks signs OIDC tokens with

    @model_validator(mode="after")
    def _check_prod(self) -> "Settings":
        if self.env == "prod":
            if self.jwt_secret.get_secret_value() == _DEV_JWT_SECRET:
                raise ValueError("JWT_SECRET must be set in prod")
            if self.task_backend == "cloudtasks" and not (
                self.gcp_project and self.tasks_service_url and self.tasks_invoker_sa
            ):
                raise ValueError("cloudtasks backend needs GCP_PROJECT, TASKS_SERVICE_URL, "
                                 "TASKS_INVOKER_SA")
        return self

    def daily_token_limit(self, plan: str) -> int:
        return {
            "guest": self.guest_daily_tokens,
            "free": self.free_daily_tokens,
            "pro": self.pro_daily_tokens,
        }[plan]


@lru_cache
def get_settings() -> Settings:
    return Settings()
