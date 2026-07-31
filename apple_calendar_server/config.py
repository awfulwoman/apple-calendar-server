from typing import Annotated
from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _split_csv(v):
    return [t.strip() for t in v.split(",") if t.strip()] if isinstance(v, str) else v


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CALENDAR_SERVER_",
        env_file=".env",
        env_file_encoding="utf-8",
    )

    host: str = "127.0.0.1"
    port: int = 4101
    bearer_tokens: Annotated[list[str], NoDecode] = []
    db_path: str = "~/.local/state/apple-calendar-server/meta.db"
    default_calendar: str = "Calendar"
    default_window_past_days: int = 30
    default_window_future_days: int = 365

    @field_validator("bearer_tokens", mode="before")
    @classmethod
    def _split_tokens(cls, v):
        return _split_csv(v)
