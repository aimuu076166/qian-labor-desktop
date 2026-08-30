from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    app_secret: str = "development-only-secret"
    pii_hash_pepper: str = ""
    database_url: str = "sqlite:///./qian_labor.db"
    redis_url: str = "redis://redis:6379/0"
    storage_root: str = "./data/uploads"
    raw_file_ttl_hours: int = 24
    ai_provider: str = "fake"
    ai_api_key: str = ""
    ai_base_url: str = "https://api.openai.com/v1"
    ai_text_model: str = ""
    ai_vision_model: str = ""
    ai_batch_budget_usd: float = 5.0
    ai_max_calls_per_analysis: int = 100
    upload_access_code: str = ""
    access_session_secret: str = ""
    processing_inline: bool = False
    allow_purge_all: bool = False
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    @property
    def effective_pii_hash_pepper(self) -> str:
        return self.pii_hash_pepper or self.app_secret


@lru_cache
def get_settings() -> Settings:
    return Settings()
