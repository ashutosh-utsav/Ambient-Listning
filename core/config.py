"""
This module handle configration settings for the app.
it loades evrything from a .env file, and uses lru_cache to cache the setting .
so that we do no have to load the .env file multiple times inside the app.
"""

from typing import ClassVar, Optional
from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache

class Settings(BaseSettings):
    openai_api_key: str
    azure_storage_connection_string: str
    azure_queue_name: str
    azure_dead_letter_queue_name: str
    azure_blob_container_name: str
    azure_table_name: str

    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str
    JWT_ISSUER: str
    ENABLE_AUDIT_LOGGING: Optional[bool] = True

    worker_memory_debug: Optional[bool] = False
    enable_vad: Optional[bool] = False

    webserver_error_callback: str

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    SAMPLING_RATE: ClassVar[int] = 16000
    TRANSCRIPTION_BUFFER_SECONDS: ClassVar[int] = 60


@lru_cache()
def get_settings() -> Settings:
    return Settings()

