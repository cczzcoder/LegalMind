"""集成测试共用的登录、MFA 与数据构造辅助函数。"""

from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pyotp

from app.core.security import CSRF_HEADER, hash_password

PASSWORD = "correct horse battery"


def sha256_hex() -> str:
    return uuid4().hex + uuid4().hex


async def seed_parse_chain(session) -> tuple:
    """建一条 org → user → source → artifact → parse_revision → chunk 的合法链路。

    返回 (organization, user, source, artifact, parse_revision, chunk)。
    """
    from app.models import Chunk, Organization, ParseRevision, Source, SourceArtifact, User

    organization = Organization(name=f"org-{uuid4()}")
    session.add(organization)
    await session.flush()

    user = User(
        organization_id=organization.id,
        username=f"u-{uuid4().hex[:12]}",
        password_hash=hash_password("test-password"),
        is_active=True,
    )
    session.add(user)
    await session.flush()

    source = Source(
        name=f"src-{uuid4().hex[:8]}",
        source_type="official",
        trust_level="high",
        license_note="测试来源",
        created_by=user.id,
    )
    session.add(source)
    await session.flush()

    artifact = SourceArtifact(
        source_id=source.id,
        object_key=uuid4().hex + uuid4().hex,
        sha256=sha256_hex(),
        size_bytes=1024,
        media_type="application/pdf",
        original_filename="example.pdf",
        sensitivity="internal",
        access_scope="organization",
        created_by=user.id,
    )
    session.add(artifact)
    await session.flush()

    parse_revision = ParseRevision(
        artifact_id=artifact.id,
        parser="test-parser",
        parser_version="1",
        config_version="v1",
        text_sha256=sha256_hex(),
        quality_status="ok",
        created_by=user.id,
    )
    session.add(parse_revision)
    await session.flush()

    chunk = Chunk(
        parse_revision_id=parse_revision.id,
        ordinal=0,
        text="第一条 测试条文",
        text_sha256=sha256_hex(),
    )
    session.add(chunk)
    await session.flush()
    return organization, user, source, artifact, parse_revision, chunk


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
