from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )

    app_env: Literal["development", "test", "production"] = "development"
    database_url: str
    # 本机 HTTP 开发时为 false；经 HTTPS 反向代理部署时必须为 true
    session_cookie_secure: bool = False

    @model_validator(mode="after")
    def validate_security_mode(self):
        if self.app_env == "production":
            raise ValueError(
                "Production is disabled until administrator MFA and "
                "object-level authorization are implemented."
            )

        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
