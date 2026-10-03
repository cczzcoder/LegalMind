"""可信代理来源 IP 集成测试（设计 11.1）：需要 TEST_DATABASE_URL。"""

import random
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.main import app
from app.models import LoginAttempt

pytestmark = pytest.mark.anyio

PROXY_IP = "10.255.0.1"


def random_ip() -> str:
    return f"203.0.{random.randint(0, 255)}.{random.randint(1, 254)}"


async def recorded_ips(session_factory, username: str) -> list[str]:
    async with session_factory() as session:
        return list(
            await session.scalars(
                select(LoginAttempt.ip_address).where(LoginAttempt.username == username)
            )
        )


async def failed_login(client: httpx.AsyncClient, username: str, forwarded_for: str):
    response = await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": "wrong password!!"},
        headers={"X-Forwarded-For": forwarded_for},
    )
    assert response.status_code == 401


def test_app_uses_configured_trusted_proxies():
    # 测试环境 TRUSTED_PROXIES 为空：中间件不信任任何来源
    [proxy] = [m for m in app.user_middleware if m.cls is ProxyHeadersMiddleware]
    assert proxy.kwargs["trusted_hosts"] == []


async def test_forwarded_for_ignored_without_trusted_proxy(make_client, session_factory):
    username = f"proxy-{uuid4().hex[:12]}"
    peer, spoofed = random_ip(), random_ip()

    async with make_client(ip=peer) as client:
        await failed_login(client, username, spoofed)

    ips = await recorded_ips(session_factory, username)
    assert peer in ips
    assert spoofed not in ips


async def test_forwarded_for_honored_from_trusted_proxy(make_client, session_factory):
    # make_client fixture 负责安装数据库依赖覆盖；这里在外层包一层可信代理中间件
    username = f"proxy-{uuid4().hex[:12]}"
    real, spoofed = random_ip(), random_ip()
    behind_proxy = ProxyHeadersMiddleware(app, trusted_hosts=[PROXY_IP])

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=behind_proxy, client=(PROXY_IP, 12345)),
        base_url="http://test",
    ) as client:
        # 客户端伪造的前缀由代理原样转发，只取最右侧的非可信地址
        await failed_login(client, username, f"{spoofed}, {real}")

    ips = await recorded_ips(session_factory, username)
    assert real in ips
    assert spoofed not in ips
    assert PROXY_IP not in ips
