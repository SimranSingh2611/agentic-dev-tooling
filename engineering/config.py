from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    engineering_database_url: str = "sqlite:///data/engineering.db"
    checkpoint_database_url: str = "sqlite:///data/checkpoints.db"
    engineering_api_key: str = "local-engineering-key"
    runner_token: str = "local-runner-key"
    runner_backend: str = "http"
    runner_url: str = "http://runner:9000"
    openai_api_key: str = ""
    openai_model: str = "gpt-4.1-mini"
    data_dir: Path = Path("data")
    workspace_dir: Path = Path("workspaces")
    source_root: Path = Path(__file__).resolve().parents[1]
    max_repairs: int = Field(default=2, ge=0, le=5)
    max_model_calls: int = Field(default=12, ge=1, le=30)
    max_clarification_rounds: int = Field(default=2, ge=1, le=5)
    model_timeout: int = Field(default=90, ge=1, le=300)
    worker_lease_seconds: int = 600


def settings() -> Settings:
    return Settings()
