"""Application configuration.

Values are read from environment variables (or a local ``.env`` file). See
``.env.example`` for the full documented list.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- General ---------------------------------------------------------
    app_name: str = "AGILE Reporting Platform"
    environment: str = "development"
    #: Returns internal exception detail to callers, so it is refused in
    #: production rather than merely discouraged.
    debug: bool = True
    api_prefix: str = "/api/v1"

    # --- Database --------------------------------------------------------
    database_url: str = "sqlite:///./storage/agile.db"
    sql_echo: bool = False

    # --- Security --------------------------------------------------------
    secret_key: str = "change-me-in-production-please-use-a-long-random-string"
    access_token_ttl_minutes: int = 720
    bootstrap_admin_email: str = "admin@agile.gov.ng"
    bootstrap_admin_password: str = "ChangeMe!2024"

    # --- Storage ---------------------------------------------------------
    upload_dir: Path = BASE_DIR / "storage" / "uploads"
    report_dir: Path = BASE_DIR / "storage" / "reports"
    max_upload_mb: int = 50

    # --- Data quality thresholds ----------------------------------------
    dqa_minimum_score: float = 60.0
    consistency_change_threshold_pct: float = 200.0
    accuracy_target_ratio_pct: float = 300.0
    outlier_zscore_threshold: float = 3.0

    # --- Logging ---------------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = True

    # --- CORS ------------------------------------------------------------
    #: Sites allowed to call the API from a browser. Empty means same-origin
    #: only, which is what the bundled dashboard needs and what a deployment
    #: should stay on unless something else genuinely calls the API. "*" is
    #: refused in production: with credentials enabled it makes the server
    #: echo back whatever origin asks.
    cors_origins: str = ""

    # --- Seeds -----------------------------------------------------------
    seed_dir: Path = BASE_DIR / "seeds"

    @field_validator("upload_dir", "report_dir", "seed_dir", mode="before")
    @classmethod
    def _expand(cls, value: object) -> object:
        if isinstance(value, str):
            path = Path(value).expanduser()
            return path if path.is_absolute() else (BASE_DIR / path).resolve()
        return value

    @property
    def cors_origin_list(self) -> list[str]:
        raw = (self.cors_origins or "").strip()
        if not raw:
            return []
        if raw == "*":
            return ["*"]
        return [origin.strip() for origin in raw.split(",") if origin.strip()]

    def production_problems(self) -> list[str]:
        """Settings that make this unsafe to expose, with what to do instead.

        Returned rather than logged, because setting ENVIRONMENT=production is
        the operator saying "this is live": a warning scrolls past in a
        container log and the deployment serves real state data signed with a
        key that is published in this repository.
        """
        problems: list[str] = []
        if self.secret_key.startswith("change-me") or len(self.secret_key) < 32:
            problems.append(
                "SECRET_KEY is the published default or too short. It signs every session "
                "token, so anyone holding it can mint an administrator session. Generate "
                'one with: python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        if self.bootstrap_admin_password == "ChangeMe!2024":
            problems.append(
                "BOOTSTRAP_ADMIN_PASSWORD is the published default. Set a real one before "
                "the administrator account is reachable from the internet."
            )
        if self.debug:
            problems.append(
                "DEBUG is on, which returns internal exception detail to callers. "
                "Set DEBUG=false."
            )
        if "*" in self.cors_origin_list:
            problems.append(
                "CORS_ORIGINS is '*'. Combined with credentials that makes the server echo "
                "back any origin that asks. Leave it empty for same-origin only, or list "
                "the exact sites that call the API."
            )
        return problems

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    def ensure_directories(self) -> None:
        for directory in (self.upload_dir, self.report_dir):
            Path(directory).mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings


settings = get_settings()
