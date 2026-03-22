"""
This module handles configuration settings for the app.
Loads from .env file, uses lru_cache for singleton behavior.

MIGRATION NOTE: Azure → AWS (S3 + DynamoDB + Redis/ARQ).
All removed Azure settings are preserved as comments below for reference.
"""

from typing import ClassVar, Optional
from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class Settings(BaseSettings):
    openai_api_key: str

    # -- AWS credentials (replaces azure_storage_connection_string) --
    aws_access_key_id: str
    aws_secret_access_key: str
    aws_region: str = "us-east-1"

    # S3 (replaces azure_blob_container_name)
    s3_bucket_name: str = "recordings"
    s3_endpoint_url: Optional[str] = None           # Set to MinIO URL for local dev

    # DynamoDB (replaces azure_table_name)
    dynamodb_table_name: str = "sessionIndex"
    dynamodb_endpoint_url: Optional[str] = None     # Set to DynamoDB Local URL for local dev

    # Redis / ARQ (replaces azure_queue_name + azure_dead_letter_queue_name)
    redis_url: str = "redis://localhost:6379"

    # -- Auth: optional API key (replaces JWT/Azure AD) --
    # Set ENABLE_API_KEY_AUTH=true and API_KEY=<strong-key> to lock down the service.
    # Disabled by default — suitable for internal microservice behind a gateway.
    enable_api_key_auth: bool = False
    api_key: Optional[str] = None

    # -- OLD Azure settings (kept for reference) --
    # azure_storage_connection_string: str
    # azure_queue_name: str
    # azure_dead_letter_queue_name: str
    # azure_blob_container_name: str
    # azure_table_name: str

    # -- OLD JWT auth settings (kept for reference) --
    # JWT_SECRET_KEY: str
    # JWT_ALGORITHM: str
    # JWT_ISSUER: str

    # -- Knowledge Base --
    kb_dynamodb_table_name: str = "clientKB"
    kb_brief_recent_sessions: int = 10  # how many sessions the brief LLM sees

    ENABLE_AUDIT_LOGGING: Optional[bool] = True
    worker_memory_debug: Optional[bool] = False
    enable_vad: Optional[bool] = False
    environment: str = "development"

    webserver_error_callback: str = "http://web-server:8000/ws/error"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    SAMPLING_RATE: ClassVar[int] = 16000
    TRANSCRIPTION_BUFFER_SECONDS: ClassVar[int] = 60


@lru_cache()
def get_settings() -> Settings:
    return Settings()
