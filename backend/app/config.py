from pydantic_settings import BaseSettings
from typing import List


class Settings(BaseSettings):
    azure_client_id: str = ""
    database_url: str = "sqlite:///./cat.db"
    cors_origins: List[str] = ["http://localhost:5173"]

    class Config:
        env_file = ".env"


settings = Settings()
