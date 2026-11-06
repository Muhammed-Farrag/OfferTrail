"""Runtime configuration (from environment / .env) and candidate preferences (JSON file).

Secrets live only in the environment or in files under `data/`, which is git-ignored.
Preferences are kept in `data/preferences.json` so `/settings` can show what is still unset.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="JOBRADAR_", env_file=".env", extra="ignore",
                                      env_ignore_empty=True)

    data_dir: Path = Path("data")

    # Source channel (read through a user-authorized MTProto client)
    source_channel: str = "software_jobs_linkedin"
    backfill_max_messages: int = 500
    backfill_days: int = 7
    telegram_api_id: int | None = None
    telegram_api_hash: SecretStr | None = None

    # Private bot
    bot_token: SecretStr | None = None
    bot_allowed_user_id: int | None = None
    bot_allowed_chat_id: int | None = None
    notify_max_per_minute: int = 15

    # LLM (Groq free tier) and local embeddings
    llm_backend: Literal["groq", "fake", "none"] = "groq"
    groq_api_key: SecretStr | None = None
    llm_model: str = "openai/gpt-oss-120b"
    llm_fallback_model: str = "openai/gpt-oss-20b"
    llm_daily_token_budget: int = 150_000  # free tier allows 200K/day; keep headroom
    llm_max_calls_per_cycle: int = 20
    embed_backend: Literal["fastembed", "hash"] = "fastembed"
    embed_model: str = "BAAI/bge-small-en-v1.5"

    # Enrichment
    fetch_enabled: bool = False  # off by default: check the site's terms before enabling
    fetch_max_per_cycle: int = 5
    fetch_timeout_s: float = 10.0
    fetch_max_bytes: int = 2_000_000

    # Gmail (send-only)
    gmail_sender: str | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jobradar.sqlite3"

    @property
    def telegram_session_path(self) -> Path:
        return self.data_dir / "telegram_reader"  # Telethon appends .session

    @property
    def gmail_client_secret_path(self) -> Path:
        return self.data_dir / "gmail_client_secret.json"

    @property
    def gmail_token_path(self) -> Path:
        return self.data_dir / "gmail_token.json"

    @property
    def prefs_path(self) -> Path:
        return self.data_dir / "preferences.json"

    @property
    def documents_dir(self) -> Path:
        return self.data_dir / "documents"

    def ensure_data_dir(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.data_dir, 0o700)


class Preferences(BaseModel):
    """Candidate preferences. `None` means "not set" and is shown as missing — never guessed."""

    full_name: str | None = None
    residence_country: str | None = None  # ISO-3166 alpha-2, e.g. "EG"
    citizenship: list[str] | None = None
    target_families: list[str] = Field(
        default_factory=lambda: ["ai_ml", "genai", "research_eng"]
    )
    include_internships: bool | None = None
    include_evaluation_gigs: bool = False  # opt-in category (annotation / AI training)
    include_data_science: bool = False
    max_years_experience: int | None = None
    languages: list[str] | None = None
    work_modes: list[Literal["remote", "hybrid", "onsite"]] | None = None
    # On-site/hybrid outside residence_country requires visa sponsorship.
    require_sponsorship_abroad: bool = True
    open_to_relocation: bool | None = None
    notify_mode: Literal["instant", "digest"] = "instant"

    def missing(self) -> list[str]:
        return [k for k, v in self.model_dump().items() if v is None]


def load_prefs(path: Path) -> Preferences:
    if not path.exists():
        return Preferences()
    return Preferences.model_validate_json(path.read_text(encoding="utf-8"))


def save_prefs(path: Path, prefs: Preferences) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(prefs.model_dump(), indent=2), encoding="utf-8")
    os.chmod(path, 0o600)
