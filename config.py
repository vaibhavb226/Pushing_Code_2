from __future__ import annotations

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Server ────────────────────────────────────────────────
    port: int = 3001
    node_env: str = "development"

    # ── Security ──────────────────────────────────────────────
    internal_api_key: SecretStr = SecretStr("")  # X-API-Key on internal routes; empty = disabled

    # ── LLM provider ─────────────────────────────────────────
    llm_provider: str = "anthropic"          # anthropic | openai | vertexai

    # Anthropic
    anthropic_api_key: SecretStr = SecretStr("")
    anthropic_model: str = "claude-sonnet-4-20250514"

    # OpenAI
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = "gpt-4o"

    # Google Vertex AI (IAM-based authentication via service account)
    # No API key needed — uses GOOGLE_APPLICATION_CREDENTIALS env var or gcloud ADC
    # Set LLM_PROVIDER=vertexai to use this provider
    vertex_model: str = "gemini-2.5-flash"
    gcp_project: str = ""                  # GCP project ID (required for Vertex AI)
    gcp_location: str = "us-central1"      # GCP region (default: us-central1)

    # ── Email provider ────────────────────────────────────────
    email_provider: str = "gmail"           # gmail | msgraph

    # Gmail / SMTP
    email_from: str = ""
    email_app_password: SecretStr = SecretStr("")
    email_host: str = "smtp.gmail.com"
    email_port: int = 587
    email_imap_host: str = "imap.gmail.com"
    email_imap_port: int = 993

    # Microsoft Graph
    ms_tenant_id: str = ""
    ms_client_id: str = ""
    ms_client_secret: SecretStr = SecretStr("")
    ms_mailbox_user_id: str = ""            # UPN of mailbox e.g. COE.BIDS@sysco.com

    # ── Database ──────────────────────────────────────────────
    db_backend: str = "json"               # json | sqlite | postgres
    database_url: str = "sqlite+aiosqlite:///./sysco_rfp.db"

    # ── SQL Query Expert (RFP AIQ chat → PostgreSQL) ──────────
    # Text-to-SQL over a separate analytics Postgres (e.g. GCP Cloud SQL).
    # Disabled by default; ai_chat_v2 falls back to normal chat when off.
    sql_expert_enabled: bool = False
    sql_expert_db_host: str = ""
    sql_expert_db_port: int = 5432
    sql_expert_db_name: str = "postgres"
    sql_expert_db_user: str = "postgres"
    sql_expert_db_password: SecretStr = SecretStr("")
    sql_expert_db_schema: str = "public"
    sql_expert_max_rows: int = 500         # safety cap on returned rows

    # ── Frontend / portal ─────────────────────────────────────
    portal_base_url: str = "http://rfp-aiq.supplier.exlservice.com:5173"
    frontend_urls: str = "http://localhost:5173,http://localhost:4173"

    @property
    def allowed_origins(self) -> list[str]:
        base = [o.strip() for o in self.frontend_urls.split(",") if o.strip()]
        # Always include portal URL as well
        if self.portal_base_url not in base:
            base.append(self.portal_base_url)
        return base

    @property
    def is_production(self) -> bool:
        return self.node_env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
