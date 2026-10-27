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

    # ---- Service (finscan.service): the API + MCP server in front of the runs ----
    #: Required by every protected endpoint (header `x-api-key`). Also signs the
    #: short-lived upload/download links unless FINSCAN_LINK_SECRET is set.
    finscan_api_key: str = ""
    finscan_link_secret: str = ""
    #: The folder holding inbox/, mappings/, models/, _work/. Empty: found by
    #: walking up from the working directory, as the CLI does.
    finscan_root: str = ""
    #: How runs are started: "local" (a process on this machine) or "azure"
    #: (a Container Apps Job execution).
    finscan_runner: str = "local"
    #: Base URL used in the links handed to users and agents.
    finscan_public_url: str = "http://localhost:8000"
    #: Host names the MCP endpoint accepts (comma-separated, "host:*" for any port).
    #: Empty: no host check (the API key still guards the endpoint).
    finscan_allowed_hosts: str = ""
    finscan_link_minutes: int = 30
    finscan_max_upload_mb: int = 200

    # ---- Azure Container Apps Job (finscan_runner = "azure") ----
    azure_subscription_id: str = ""
    azure_resource_group: str = ""
    azure_job_name: str = ""
    #: The image and container name of the Job; a run overrides only its arguments.
    azure_job_image: str = ""
    azure_job_container: str = "finscan"

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
