"""Model-provider abstraction."""

from __future__ import annotations

from agentwarden.config import Settings
from agentwarden.providers.base import ModelProvider
from agentwarden.providers.ollama import OllamaProvider
from agentwarden.providers.openai_compat import OpenAICompatProvider


class CloudNotAllowed(RuntimeError):
    pass


THINKING_FAMILIES = ("qwen3", "deepseek-r1", "gpt-oss", "magistral", "phi4-reasoning")


def resolve_think(mode: str, model: str) -> bool | None:
    """None = omit the flag (non-thinking models reject think=true and may reject the field)."""
    if mode == "on":
        return True
    if mode == "off":
        return False
    if mode == "omit":
        return None
    return False if model.lower().startswith(THINKING_FAMILIES) else None


def build_provider(settings: Settings, provider: str = "ollama", model: str | None = None) -> ModelProvider:
    if provider == "ollama":
        name = model or settings.model
        return OllamaProvider(name, settings.ollama_url, num_ctx=settings.ollama_num_ctx,
                              think=resolve_think(settings.ollama_think, name))
    if provider == "cloud":
        # Cost guard (THREAT_MODEL T12): explicit opt-in + key + model required.
        if not settings.allow_cloud:
            raise CloudNotAllowed("Cloud provider disabled. Set AW_ALLOW_CLOUD=1 only after approving cost.")
        if not (settings.cloud_api_key and settings.cloud_base_url and (model or settings.cloud_model)):
            raise CloudNotAllowed("AW_CLOUD_BASE_URL, AW_CLOUD_API_KEY and AW_CLOUD_MODEL must be set.")
        return OpenAICompatProvider(
            model or settings.cloud_model or "",
            settings.cloud_base_url,
            settings.cloud_api_key.get_secret_value(),
        )
    raise ValueError(f"unknown provider {provider!r}")
