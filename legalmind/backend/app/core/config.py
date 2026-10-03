from functools import lru_cache
from ipaddress import ip_network
from typing import Literal

from cryptography.fernet import Fernet
from pydantic import field_validator, model_validator
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
    # Fernet 密钥，用于加密 TOTP 密钥；未配置时管理员无法完成 MFA
    mfa_encryption_key: str | None = None
    # 可信反向代理的地址或网段，逗号分隔；为空时不读取 X-Forwarded-For
    trusted_proxies: str = ""
    # 原始文件存储目录（设计 7.3），相对路径按进程工作目录解析；不得作为静态目录暴露
    storage_root: str = "data/artifacts"
    max_upload_bytes: int = 50 * 1024 * 1024

    # 解析（设计 6、7、14.2）。PDF 双后端可互换；选型待样本基准按 §6 精度要求拍板
    parsing_pdf_backend: Literal["pypdfium2", "pdfplumber"] = "pypdfium2"
    # 单次解析的资源上限，超限主动拒绝/中止，避免 16 GB 单机 OOM
    parse_max_bytes: int = 50 * 1024 * 1024
    parse_max_pages: int = 2000
    parse_max_chars: int = 5_000_000
    # 解析期间进程内存增长预算（MB）；<=0 表示不限制
    parse_memory_budget_mb: int = 1024

    # 后台任务 worker（设计 12.2、14.2）：单并发、按需启动，任务结束即释放资源
    worker_lease_seconds: int = 300
    worker_poll_seconds: float = 5.0
    # 失败重试的指数退避：base * 2^(attempt-1)，封顶 max
    worker_backoff_base_seconds: int = 30
    worker_backoff_max_seconds: int = 900

    @field_validator("mfa_encryption_key", mode="before")
    @classmethod
    def empty_key_is_unset(cls, value):
        return value or None

    @property
    def trusted_proxy_list(self) -> list[str]:
        return [item.strip() for item in self.trusted_proxies.split(",") if item.strip()]

    @model_validator(mode="after")
    def validate_security_mode(self):
        # 配置错误在启动时暴露，而不是在首次登录时
        for item in self.trusted_proxy_list:
            try:
                # strict 与 uvicorn 的解析一致；主机位非零的网段会被它当作字面量而失效
                ip_network(item)
            except ValueError:
                raise ValueError(f"Invalid TRUSTED_PROXIES entry: {item!r}") from None

        if self.mfa_encryption_key is not None:
            try:
                Fernet(self.mfa_encryption_key)
            except ValueError:
                raise ValueError("MFA_ENCRYPTION_KEY must be a Fernet key.") from None

        if self.app_env == "production":
            if self.mfa_encryption_key is None:
                raise ValueError("MFA_ENCRYPTION_KEY is required in production.")
            if not self.session_cookie_secure:
                raise ValueError("SESSION_COOKIE_SECURE must be true in production.")

        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
