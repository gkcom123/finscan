"""Central configuration. All tunables live here, sourced from env / .env."""
from __future__ import annotations

from pathlib import Path

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
    openai_model: str = "gpt-4o"
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

    # Extraction
    finscan_ocr_fallback: bool = True
    finscan_max_pdf_chars: int = 120_000
    finscan_fuzzy_threshold: int = 86
    finscan_tolerance_pct: float = 1.0
    finscan_min_confidence: float = 0.60

    # Layout profiles
    finscan_profile_store: Path = Path("./profiles")
    # Default to no confirmation gate so runs always proceed to write.
    finscan_require_confirmation: bool = False

    # API
    finscan_api_key: str = "change-me"
    finscan_work_dir: Path = Path("./_work")

    # CLI company runs: PDF + model from inbox/<company>/; output inbox/<company>_output.*
    finscan_inbox_dir: Path = Path("./inbox")

    # Local demo: Copilot reads PDF + model from test/<company>/ on disk
    finscan_test_dir: Path = Path("./test")
    finscan_demo_enabled: bool = True

    @property
    def configured(self) -> bool:
        if self.finscan_llm_provider.lower() == "azure":
            return bool(self.azure_openai_endpoint and self.azure_openai_api_key)
        return bool(self.openai_api_key)


settings = Settings()
