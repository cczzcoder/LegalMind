import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.models import AuthSession, User, UserRole

SESSION_COOKIE = "legalmind_session"
CSRF_HEADER = "X-CSRF-Token"
SESSION_IDLE_TIMEOUT = timedelta(minutes=30)
SESSION_MAX_AGE = timedelta(hours=12)
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

password_hasher = PasswordHasher()  # Argon2id，使用库的推荐参数
# 用户名不存在时也做一次校验，避免通过响应时间判断账号是否存在
_DUMMY_HASH = password_hasher.hash("legalmind-timing-placeholder")


@dataclass(frozen=True)
class Principal:
    organization_id: UUID
    user_id: UUID
    roles: frozenset[str]
    session_id: UUID | None = None


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        password_hasher.verify(password_hash or _DUMMY_HASH, password)
    except VerificationError:
        return False
    return password_hash is not None


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def utcnow() -> datetime:
    return datetime.now(UTC)


async def get_principal(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Principal:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    now = utcnow()

    # 独立提交，后续业务代码可以开启自己的事务
    async with session.begin():
        row = (
            await session.execute(
                select(AuthSession, User)
                .join(User, User.id == AuthSession.user_id)
                .where(AuthSession.token_hash == hash_token(token))
            )
        ).one_or_none()

        if row is None:
            raise HTTPException(status_code=401, detail="Not authenticated")

        auth_session, user = row
        if (
            auth_session.revoked_at is not None
            or auth_session.expires_at <= now
            or auth_session.last_seen_at + SESSION_IDLE_TIMEOUT <= now
            or not user.is_active
        ):
            raise HTTPException(status_code=401, detail="Session expired")

        if request.method not in SAFE_METHODS and not secrets.compare_digest(
            request.headers.get(CSRF_HEADER, "").encode("utf-8"),
            auth_session.csrf_token.encode("utf-8"),
        ):
            raise HTTPException(status_code=403, detail="CSRF token missing or invalid")

        # 每次请求重新读取角色，权限变更立即生效
        roles = await session.scalars(
            select(UserRole.role).where(UserRole.user_id == user.id)
        )
        auth_session.last_seen_at = now

        return Principal(
            organization_id=user.organization_id,
            user_id=user.id,
            roles=frozenset(roles),
            session_id=auth_session.id,
        )
