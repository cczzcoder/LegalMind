from functools import lru_cache
from typing import Literal
from uuid import UUID

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )

    app_env: Literal["development", "test", "production"] = "development"
    database_url: str
    dev_api_key: SecretStr
    dev_org_id: UUID
    dev_user_id: UUID

    @model_validator(mode="after")
    def validate_security_mode(self):
        if self.app_env == "production":
            raise ValueError(
                "Production is disabled until real authentication "
                "and authorization are implemented."
            )

        key = self.dev_api_key.get_secret_value()
        if len(key) < 32 or key.startswith("REPLACE"):
            raise ValueError("Configure a random DEV_API_KEY.")

        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
