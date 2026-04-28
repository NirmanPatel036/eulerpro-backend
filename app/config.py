from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), extra="ignore")

    # Supabase
    SUPABASE_URL: str = "https://eejyderifolfefbsrfei.supabase.co"
    SUPABASE_SERVICE_ROLE_KEY: str = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImVlanlkZXJpZm9sZmVmYnNyZmVpIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3MjAxMDcyMSwiZXhwIjoyMDg3NTg2NzIxfQ.QgYkyTHm_7GbgRO4iQiaomnxXCgEw1x0SeQ0kEkQO1s"

    # Resend
    RESEND_API_KEY: str = "re_URFszp5K_JoqeSzQFxqHW1tH9pj8CmnMJ"
    EMAIL_FROM: str = "updates@diploscribe.me"

    # App
    FRONTEND_URL: str = "http://localhost:3000"
    PROCTORING_SERVICE_URL: str = "http://localhost:5001"

    # CORS
    ALLOWED_ORIGINS: list[str] = ["http://localhost:3000"]


settings = Settings()
