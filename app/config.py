from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), extra="ignore")

    # Supabase
    SUPABASE_URL: str
    SUPABASE_SERVICE_ROLE_KEY: str

    # Resend
    RESEND_API_KEY: str
    EMAIL_FROM: str

    # App
    FRONTEND_URL: str
    PROCTORING_SERVICE_URL: str

    # CORS
    ALLOWED_ORIGINS: list[str]


settings = Settings()
