import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.core.config import Settings

KEY = Fernet.generate_key().decode()


def settings(**overrides) -> Settings:
    values = {
        "database_url": "postgresql+asyncpg://unused:unused@localhost/unused",
        "app_env": "production",
        "mfa_encryption_key": KEY,
        "session_cookie_secure": True,
        "trusted_proxies": "",
        **overrides,
    }
    return Settings(_env_file=None, **values)


def test_production_accepts_hardened_config():
    config = settings(trusted_proxies=" 10.0.0.1 , 172.16.0.0/12 ")

    assert config.trusted_proxy_list == ["10.0.0.1", "172.16.0.0/12"]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"mfa_encryption_key": ""}, "MFA_ENCRYPTION_KEY is required"),
        ({"session_cookie_secure": False}, "SESSION_COOKIE_SECURE must be true"),
        ({"mfa_encryption_key": "not-a-key"}, "must be a Fernet key"),
        ({"trusted_proxies": "10.0.0.1, proxy.local"}, "Invalid TRUSTED_PROXIES"),
        # 主机位非零的网段在 uvicorn 中不会生效，启动时拒绝
        ({"trusted_proxies": "10.0.0.1/8"}, "Invalid TRUSTED_PROXIES"),
    ],
)
def test_insecure_or_invalid_config_is_rejected(overrides, message):
    with pytest.raises(ValidationError, match=message):
        settings(**overrides)


def test_development_allows_missing_mfa_key_and_plain_cookie():
    config = settings(app_env="development", mfa_encryption_key="", session_cookie_secure=False)

    assert config.mfa_encryption_key is None
