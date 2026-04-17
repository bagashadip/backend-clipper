from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    database_url: str = "postgresql://postgres:postgres@localhost:5432/clipper"
    redis_url: str = "redis://localhost:6379/0"
    gemini_api_key: str = ""
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_s3_bucket: str = "clipper-output"
    aws_s3_region: str = "us-east-1"
    s3_endpoint_url: str = ""

    # Google OAuth
    google_client_id: str = "774266853892-n6ep9i661fble252slbjhdspb8nnd7bf.apps.googleusercontent.com"
    google_client_secret: str = "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"
    
    # JWT
    secret_key: str = "change-me-in-production"
    frontend_url: str = "http://localhost:3000"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
