from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    database_url: str = "postgresql://postgres:postgres@localhost:5432/clipper"
    redis_url: str = "redis://localhost:6379/0"
    gemini_api_key: str = ""
    # DeepSeek (OpenAI-compatible)
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"  # e.g. "deepseek-chat", "deepseek-v4-pro"
    # AI provider switch: "gemini" or "deepseek"
    ai_provider: str = "deepseek"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_s3_bucket: str = "clipper-output"
    aws_s3_region: str = "us-east-1"
    s3_endpoint_url: str = ""

    # Google OAuth
    google_client_id: str = ""
    google_client_secret: str = ""

    # JWT — must be overridden in production via SECRET_KEY env var
    secret_key: str = "change-me-in-production"

    # Midtrans Integration
    midtrans_server_key: str = ""

    # Public-facing URLs
    # Set BACKEND_PUBLIC_URL to the HTTPS URL of the backend in production
    # so Google OAuth redirect URIs are correct.
    # e.g. https://api.example.com  or  https://34.177.101.239 (with SSL proxy)
    backend_public_url: str = "http://localhost:8000"
    frontend_url: str = "http://localhost:3000"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
