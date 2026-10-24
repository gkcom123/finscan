"""Model-provider configuration, sourced from the environment / `.env`."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # LLM provider: "openai" or "azure"
    finscan_llm_provider: str = "openai"
    llm_temperature: float = 0.0

    # OpenAI
    openai_api_key: str = ""
    openai_model: str = "gpt-5.4"
    openai_base_url: str = ""

    # Azure OpenAI
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_api_version: str = "2024-10-21"
    azure_openai_deployment: str = "gpt-4o"

    # TLS: only for local dev behind a corporate SSL-inspecting proxy that
    # re-signs traffic with an untrusted root CA. Disables certificate
    # verification on LLM API calls — never enable this in production.
    finscan_insecure_ssl: bool = False

    @property
    def configured(self) -> bool:
        if self.finscan_llm_provider.lower() == "azure":
            return bool(self.azure_openai_endpoint and self.azure_openai_api_key)
        return bool(self.openai_api_key)

    @property
    def model_name(self) -> str:
        """The model target the configured provider actually calls."""
        if self.finscan_llm_provider.lower() == "azure":
            return self.azure_openai_deployment
        return self.openai_model


settings = Settings()
