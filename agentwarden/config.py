"""Configuration: environment settings (secrets, endpoints) and per-run agent configuration.

Environment settings come from ``AW_*`` variables / ``.env``. Agent configuration is a plain
Pydantic model so it can be snapshotted verbatim into ``executions`` and ``eval_runs``.
"""

from __future__ import annotations

from urllib.parse import quote_plus

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AW_", env_file=".env", extra="ignore")

    mongo_uri: SecretStr | None = None
    mongo_user: str = "agentwarden"
    mongo_password: SecretStr | None = None
    mongo_host: str = "127.0.0.1:27017"
    mongo_db: str = "agentwarden"

    ollama_url: str = "http://127.0.0.1:11434"
    model: str = "qwen3:4b"
    ollama_num_ctx: int = 8192
    ollama_think: bool | None = False  # None = don't send the flag (non-thinking models)

    allow_cloud: bool = False
    cloud_base_url: str | None = None
    cloud_api_key: SecretStr | None = None
    cloud_model: str | None = None

    def resolved_mongo_uri(self) -> str:
        if self.mongo_uri is not None:
            return self.mongo_uri.get_secret_value()
        if self.mongo_password is None:
            return f"mongodb://{self.mongo_host}/?directConnection=true"
        pw = quote_plus(self.mongo_password.get_secret_value())
        return (
            f"mongodb://{quote_plus(self.mongo_user)}:{pw}@{self.mongo_host}/"
            "?directConnection=true&authSource=admin"
        )


class Limits(BaseModel):
    max_steps: int = Field(8, ge=1, description="Max model calls per execution")
    max_tool_calls: int = Field(10, ge=0)
    deadline_s: float = Field(240.0, gt=0, description="Wall-clock budget for the whole execution")
    max_total_tokens: int = Field(60_000, ge=1)
    max_consecutive_tool_errors: int = Field(4, ge=1)


class RetryPolicy(BaseModel):
    enabled: bool = True
    max_attempts: int = Field(3, ge=1)
    base_delay_s: float = 0.2
    max_delay_s: float = 2.0


class ReliabilityConfig(BaseModel):
    retries: RetryPolicy = RetryPolicy()
    loop_detection: bool = True
    loop_repeat_limit: int = Field(3, ge=2, description="Identical successful calls that terminate the run")


class GuardrailsConfig(BaseModel):
    enabled: bool = True  # False = shadow mode: policies evaluated + logged, not enforced
    spotlighting: bool = True


class FaultConfig(BaseModel):
    failure_rate: float = Field(0.0, ge=0.0, le=1.0)
    latency_ms: int = Field(0, ge=0)
    tools: list[str] | None = None  # None = all tools

    @property
    def active(self) -> bool:
        return self.failure_rate > 0 or self.latency_ms > 0


class AgentConfig(BaseModel):
    agent_id: str
    description: str = ""
    tools: list[str]
    capabilities: list[str]
    limits: Limits = Limits()
    reliability: ReliabilityConfig = ReliabilityConfig()
    guardrails: GuardrailsConfig = GuardrailsConfig()
    temperature: float = 0.0
    seed: int | None = 7
