"""集成测试共用的登录与 MFA 辅助函数。"""

from datetime import UTC, datetime

import httpx
import pyotp

from app.core.security import CSRF_HEADER

PASSWORD = "correct horse battery"

# 用户名 -> (TOTP 密钥, 最近使用的时间步)，模拟用户手中的认证器
AUTHENTICATORS: dict[str, tuple[str, int]] = {}


def next_totp(username: str) -> str:
    # 已用过的时间步会被拒绝，同一时间窗内再次登录时使用下一个时间步（服务端允许 +1 偏差）
    secret, last = AUTHENTICATORS[username]
    totp = pyotp.TOTP(secret)
    timecode = max(totp.timecode(datetime.now(UTC)), last + 1)
    AUTHENTICATORS[username] = (secret, timecode)
    return totp.generate_otp(timecode)


async def enroll_mfa(client: httpx.AsyncClient, username: str) -> list[str]:
    enrollment = await client.post("/api/v1/auth/mfa/enroll")
    assert enrollment.status_code == 200
    AUTHENTICATORS[username] = (enrollment.json()["secret"], -1)
    confirmed = await client.post("/api/v1/auth/mfa/confirm", json={"code": next_totp(username)})
    assert confirmed.status_code == 200
    return confirmed.json()["recovery_codes"]


async def login(
    client: httpx.AsyncClient,
    username: str,
    password: str = PASSWORD,
    complete_mfa: bool = True,
):
    """登录；complete_mfa 为真时按服务端要求自动绑定或验证 TOTP。"""
    response = await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    if response.status_code == 200:
        client.headers[CSRF_HEADER] = response.json()["csrf_token"]
        status = response.json()["mfa_status"]
        if complete_mfa and status == "enroll":
            await enroll_mfa(client, username)
        elif complete_mfa and status == "verify":
            verified = await client.post(
                "/api/v1/auth/mfa/verify", json={"code": next_totp(username)}
            )
            assert verified.status_code == 204
    return response


async def create_page(client: httpx.AsyncClient, **extra):
    return await client.post(
        "/api/v1/wiki/pages",
        json={"title": "测试页面", "body": "正文", **extra},
    )
