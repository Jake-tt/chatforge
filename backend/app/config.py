from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.services.llm import TokenWeights

_DEV_JWT_SECRET = "dev-only-secret-change-me-0123456789"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: Literal["dev", "test", "prod"] = "dev"

    database_url: str = "postgresql+asyncpg://chatforge:chatforge@localhost:5432/chatforge"
    redis_url: str = "redis://localhost:6379/0"
    cors_origins: list[str] = ["http://localhost:5173"]
    log_format: Literal["text", "json"] = "text"  # "json" on Cloud Run

    jwt_secret: SecretStr = SecretStr(_DEV_JWT_SECRET)
    user_token_ttl_minutes: int = 60 * 24
    guest_token_ttl_minutes: int = 60 * 24 * 30

    # LLM
    llm_provider: Literal["fake", "anthropic", "ollama"] = "fake"
    anthropic_api_key: SecretStr | None = None  # ANTHROPIC_API_KEY; Secret Manager in prod
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3:8b"  # native tool calling; fits a 16GB Apple Silicon machine
    ollama_num_ctx: int = 8192
    anthropic_model: str = "claude-opus-5-5"
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    llm_max_tokens: int = 16000
    llm_aux_model: str = "claude-opus-5-5"  # router, summaries, titles
    history_limit: int = 20
    system_prompt: str = "You are a concise, friendly assistant. Answer in the user's language."

    # RAG over the help-center knowledge base
    rag_enabled: bool = True
    knowledge_dir: str = str(Path(__file__).resolve().parent.parent / "knowledge")
    search_top_k: int = 4
    router_timeout_seconds: float = 8.0
    search_timeout_seconds: float = 5.0
    account_context_enabled: bool = True  # cite the user's plan/usage when the router asks
    past_search_enabled: bool = True  # search the user's earlier conversations when asked
    past_search_conversations: int = 20  # most recently active ones are candidates
    past_search_top_k: int = 3
    search_after_turns: int = 3  # from this many user turns on, search whenever a query exists
    grounding_char_budget: int = 1500  # longer source text is compressed
    summary_char_limit: int = 1000

    # Rolling summary of long conversations (background task)
    summary_enabled: bool = True
    summary_trigger_messages: int = 16  # keep below history_limit so nothing falls off
    summary_trigger_tokens: int = 6000  # estimated, over the unsummarized messages
    summary_keep_turns: int = Field(default=2, ge=1)  # latest user turns sent verbatim
    summary_batch_messages: int = 40  # most messages folded per run; backlogs take several
    conversation_summary_char_limit: int = 2000
    summary_timeout_seconds: float = 30.0

    # LangGraph agent (router → coordinator / ReAct tools). The legacy fixed pipeline runs
    # only with this off, and is removed in the next PR.
    agent_enabled: bool = True
    agent_max_tool_rounds: int = Field(default=3, ge=0, le=5)  # bounds cost per turn
    agent_max_tool_calls: int = Field(default=4, ge=1, le=8)  # per round
    agent_timeout_seconds: float = 60.0  # per agent model call
    # Whole turn (router, lookups, every agent round). Below Cloud Run's request timeout,
    # so a slow turn ends with an `error` event instead of a cut-off stream.
    agent_turn_timeout_seconds: float = 240.0

    # Follow-up question chips under each answer
    suggestions_enabled: bool = True
    suggestions_timeout_seconds: float = 6.0

    # Quotas — tokens per calendar day in `quota_timezone`
    quota_timezone: str = "Asia/Seoul"
    guest_daily_tokens: int = 20_000
    free_daily_tokens: int = 200_000
    pro_daily_tokens: int = 2_000_000
    # Quotas count input-token equivalents: each token kind is weighted by its price
    # relative to an uncached input token (defaults: claude-opus-5-5 list prices).
    token_weight_output: float = 5.0
    token_weight_cache_read: float = 0.05
    token_weight_cache_write: float = 1.25  # 5-minute TTL
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
            # Fail at startup, not on the first chat request.
            if self.llm_provider == "anthropic" and not self.anthropic_api_key:
                raise ValueError("LLM_PROVIDER=anthropic needs ANTHROPIC_API_KEY in prod")
            if self.llm_provider == "ollama" and any(
                host in self.ollama_base_url for host in ("localhost", "127.0.0.1")
            ):
                raise ValueError("LLM_PROVIDER=ollama needs a reachable OLLAMA_BASE_URL in prod")
        return self

    def token_weights(self) -> TokenWeights:
        return TokenWeights(self.token_weight_output, self.token_weight_cache_read,
                            self.token_weight_cache_write)

    def daily_token_limit(self, plan: str) -> int:
        return {
            "guest": self.guest_daily_tokens,
            "free": self.free_daily_tokens,
            "pro": self.pro_daily_tokens,
        }[plan]


@lru_cache
def get_settings() -> Settings:
    return Settings()
