from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import (
    SESSION_MAX_AGE,
    Principal,
    hash_password,
    hash_token,
    is_mfa_pending,
    mfa_status,
    new_token,
    utcnow,
    verify_password,
)
from app.models import AuditEvent, AuthSession, LoginAttempt, User, UserRole
from app.modules.identity.schemas import CreateUser, UserOut

LOCKOUT_WINDOW = timedelta(minutes=15)
MAX_FAILURES_PER_USERNAME = 5
MAX_FAILURES_PER_IP = 20


@dataclass(frozen=True)
class NewSession:
    token: str
    csrf_token: str
    user: UserOut
    mfa_status: str


def audit(
    session: AsyncSession,
    organization_id: UUID,
    actor_id: UUID | None,
    action: str,
    resource_id: UUID,
    payload: dict | None = None,
) -> None:
    # 审计正文不写入密码、会话令牌或 CSRF 令牌
    session.add(
        AuditEvent(
            organization_id=organization_id,
            actor_id=actor_id,
            action=action,
            resource_id=resource_id,
            payload=payload or {},
        )
    )


async def user_out(session: AsyncSession, user: User) -> UserOut:
    roles = await session.scalars(
        select(UserRole.role).where(UserRole.user_id == user.id).order_by(UserRole.role)
    )
    return UserOut(
        id=user.id,
        organization_id=user.organization_id,
        username=user.username,
        is_active=user.is_active,
        roles=list(roles),
        mfa_enabled=user.mfa_enabled_at is not None,
    )


async def recent_failures(session: AsyncSession, column, value: str) -> int:
    return await session.scalar(
        select(func.count())
        .select_from(LoginAttempt)
        .where(
            column == value,
            LoginAttempt.succeeded.is_(False),
            LoginAttempt.created_at > utcnow() - LOCKOUT_WINDOW,
        )
    )


async def login(
    session: AsyncSession,
    username: str,
    password: str,
    ip_address: str,
) -> NewSession:
    # 锁定按用户名字符串计数，与账号是否存在无关，不泄露存在性
    async with session.begin():
        if (
            await recent_failures(session, LoginAttempt.username, username)
            >= MAX_FAILURES_PER_USERNAME
            or await recent_failures(session, LoginAttempt.ip_address, ip_address)
            >= MAX_FAILURES_PER_IP
        ):
            raise HTTPException(
                status_code=429,
                detail="Too many failed attempts; try again later",
            )

        user = await session.scalar(select(User).where(User.username == username))
        # 即便 user 为 None 也执行一次哈希校验，保持时间恒定（verify_password 用 dummy hash 兜底）
        password_ok = verify_password(user.password_hash if user else None, password)
        # 显式判空，不依赖短路求值规避 user 为 None（避免改动时引入 AttributeError）
        succeeded = password_ok and user is not None and user.is_active

        session.add(
            LoginAttempt(
                username=username,
                ip_address=ip_address,
                succeeded=succeeded,
            )
        )

        if not succeeded:
            if user is not None:
                audit(session, user.organization_id, None, "auth.login.failed", user.id)
            result = None
        else:
            token, csrf_token = new_token(), new_token()
            now = utcnow()
            auth_session = AuthSession(
                user_id=user.id,
                token_hash=hash_token(token),
                csrf_token=csrf_token,
                last_seen_at=now,
                expires_at=now + SESSION_MAX_AGE,
            )
            session.add(auth_session)
            audit(session, user.organization_id, user.id, "auth.login.succeeded", user.id)
            out = await user_out(session, user)
            pending = is_mfa_pending(user, frozenset(out.roles), auth_session)
            result = NewSession(token, csrf_token, out, mfa_status(user, pending))

    # 失败记录已提交后再返回统一错误
    if result is None:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    return result


async def logout(session: AsyncSession, principal: Principal) -> None:
    async with session.begin():
        await session.execute(
            update(AuthSession)
            .where(AuthSession.id == principal.session_id)
            .values(revoked_at=utcnow())
        )
        audit(
            session,
            principal.organization_id,
            principal.user_id,
            "auth.logout",
            principal.user_id,
        )


async def get_org_user(
    session: AsyncSession,
    principal: Principal,
    user_id: UUID,
    lock: bool = False,
) -> User:
    statement = select(User).where(
        User.id == user_id,
        User.organization_id == principal.organization_id,
    )
    if lock:
        statement = statement.with_for_update()

    user = await session.scalar(statement)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


async def create_user(
    session: AsyncSession,
    organization_id: UUID,
    actor_id: UUID | None,
    data: CreateUser,
) -> UserOut:
    async with session.begin():
        if await session.scalar(select(User.id).where(User.username == data.username)):
            raise HTTPException(status_code=409, detail="Username already exists")

        user = User(
            organization_id=organization_id,
            username=data.username,
            password_hash=hash_password(data.password),
            is_active=True,
        )
        session.add(user)
        await session.flush()

        session.add_all(UserRole(user_id=user.id, role=role) for role in data.roles)
        audit(
            session,
            organization_id,
            actor_id,
            "user.created",
            user.id,
            {"username": user.username, "roles": data.roles},
        )
        await session.flush()
        return await user_out(session, user)


async def set_roles(
    session: AsyncSession,
    principal: Principal,
    user_id: UUID,
    roles: list[str],
) -> UserOut:
    if user_id == principal.user_id and "system_admin" not in roles:
        raise HTTPException(status_code=400, detail="Cannot remove your own admin role")

    async with session.begin():
        user = await get_org_user(session, principal, user_id, lock=True)
        before = (await user_out(session, user)).roles

        await session.execute(delete(UserRole).where(UserRole.user_id == user.id))
        session.add_all(UserRole(user_id=user.id, role=role) for role in roles)
        audit(
            session,
            principal.organization_id,
            principal.user_id,
            "user.roles.changed",
            user.id,
            {"before": before, "after": roles},
        )
        await session.flush()
        return await user_out(session, user)


async def set_active(
    session: AsyncSession,
    principal: Principal,
    user_id: UUID,
    is_active: bool,
) -> UserOut:
    if user_id == principal.user_id and not is_active:
        raise HTTPException(status_code=400, detail="Cannot disable yourself")

    async with session.begin():
        user = await get_org_user(session, principal, user_id, lock=True)
        user.is_active = is_active

        if not is_active:
            # 禁用立即吊销全部会话
            await session.execute(
                update(AuthSession)
                .where(
                    AuthSession.user_id == user.id,
                    AuthSession.revoked_at.is_(None),
                )
                .values(revoked_at=utcnow())
            )

        audit(
            session,
            principal.organization_id,
            principal.user_id,
            "user.enabled" if is_active else "user.disabled",
            user.id,
        )
        await session.flush()
        return await user_out(session, user)


async def list_users(session: AsyncSession, principal: Principal) -> list[UserOut]:
    users = await session.scalars(
        select(User)
        .where(User.organization_id == principal.organization_id)
        .order_by(User.username)
    )
    return [await user_out(session, user) for user in users]
