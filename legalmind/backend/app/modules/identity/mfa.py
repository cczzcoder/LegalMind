"""TOTP 第二因素（FR-10“管理员强认证”、设计 11.1）。

TOTP 密钥以 Fernet 加密保存；恢复码只保存 SHA-256；已用过的时间步不能再次使用。
"""

import hmac
import secrets
from dataclasses import dataclass
from uuid import UUID

import pyotp
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import Principal, hash_token, utcnow
from app.models import AuditEvent, AuthSession, MfaRecoveryCode, User
from app.modules.identity.service import LOCKOUT_WINDOW, audit

ISSUER = "LegalMind"
RECOVERY_CODE_COUNT = 10
MAX_MFA_FAILURES = 5
MFA_FAILED = "auth.mfa.failed"


@dataclass(frozen=True)
class Enrollment:
    secret: str
    otpauth_uri: str


def fernet() -> Fernet:
    key = get_settings().mfa_encryption_key
    if key is None:
        raise HTTPException(status_code=503, detail="MFA is not configured")
    return Fernet(key)


def decrypt_secret(user: User) -> str:
    try:
        return fernet().decrypt(user.mfa_secret_encrypted.encode()).decode()
    except InvalidToken:
        # 密钥被更换或数据损坏：拒绝而不是放行
        raise HTTPException(status_code=503, detail="MFA secret unavailable") from None


def match_totp(secret: str, code: str, last_timecode: int | None) -> int | None:
    """返回匹配的时间步；允许前后各一个时间步的时钟偏差。"""
    totp = pyotp.TOTP(secret)
    current = totp.timecode(utcnow())
    for timecode in (current - 1, current, current + 1):
        if last_timecode is not None and timecode <= last_timecode:
            continue
        if hmac.compare_digest(totp.generate_otp(timecode), code):
            return timecode
    return None


def normalize_recovery_code(code: str) -> str:
    return code.replace("-", "").replace(" ", "").lower()


def new_recovery_codes() -> list[str]:
    codes = [secrets.token_hex(5) for _ in range(RECOVERY_CODE_COUNT)]
    return [f"{code[:5]}-{code[5:]}" for code in codes]


async def lock_user(session: AsyncSession, user_id: UUID) -> User:
    return await session.scalar(
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def check_failures(session: AsyncSession, user_id: UUID) -> None:
    failures = await session.scalar(
        select(func.count())
        .select_from(AuditEvent)
        .where(
            AuditEvent.actor_id == user_id,
            AuditEvent.action == MFA_FAILED,
            AuditEvent.created_at > utcnow() - LOCKOUT_WINDOW,
        )
    )
    if failures >= MAX_MFA_FAILURES:
        raise HTTPException(status_code=429, detail="Too many failed attempts; try again later")


async def mark_session_verified(session: AsyncSession, principal: Principal) -> None:
    await session.execute(
        update(AuthSession)
        .where(AuthSession.id == principal.session_id)
        .values(mfa_verified_at=utcnow())
    )


async def start_enrollment(session: AsyncSession, principal: Principal) -> Enrollment:
    cipher = fernet()
    async with session.begin():
        user = await lock_user(session, principal.user_id)
        if user.mfa_enabled_at is not None:
            raise HTTPException(status_code=409, detail="MFA already enabled")

        # 未确认前可重新开始，旧的待确认密钥被替换
        secret = pyotp.random_base32()
        user.mfa_secret_encrypted = cipher.encrypt(secret.encode()).decode()
        audit(session, user.organization_id, user.id, "user.mfa.enrollment_started", user.id)

    uri = pyotp.TOTP(secret).provisioning_uri(name=user.username, issuer_name=ISSUER)
    return Enrollment(secret=secret, otpauth_uri=uri)


async def confirm_enrollment(
    session: AsyncSession,
    principal: Principal,
    code: str,
) -> list[str]:
    async with session.begin():
        user = await lock_user(session, principal.user_id)
        if user.mfa_enabled_at is not None:
            raise HTTPException(status_code=409, detail="MFA already enabled")
        if user.mfa_secret_encrypted is None:
            raise HTTPException(status_code=409, detail="Start enrollment first")
        await check_failures(session, user.id)

        timecode = match_totp(decrypt_secret(user), code.strip(), None)
        if timecode is None:
            audit(session, user.organization_id, user.id, MFA_FAILED, user.id)
            codes = None
        else:
            user.mfa_enabled_at = utcnow()
            user.mfa_last_timecode = timecode
            codes = new_recovery_codes()
            await session.execute(
                delete(MfaRecoveryCode).where(MfaRecoveryCode.user_id == user.id)
            )
            session.add_all(
                MfaRecoveryCode(user_id=user.id, code_hash=hash_token(normalize_recovery_code(c)))
                for c in codes
            )
            await mark_session_verified(session, principal)
            audit(session, user.organization_id, user.id, "user.mfa.enabled", user.id)

    # 失败记录已提交后再返回错误；用 400 而非 401，会话本身仍然有效
    if codes is None:
        raise HTTPException(status_code=400, detail="Invalid code")
    return codes


async def verify(session: AsyncSession, principal: Principal, code: str) -> None:
    async with session.begin():
        user = await lock_user(session, principal.user_id)
        if user.mfa_enabled_at is None:
            raise HTTPException(status_code=409, detail="MFA not enrolled")
        await check_failures(session, user.id)

        method = None
        timecode = match_totp(decrypt_secret(user), code.strip(), user.mfa_last_timecode)
        if timecode is not None:
            user.mfa_last_timecode = timecode
            method = "totp"
        else:
            recovery = await session.scalar(
                select(MfaRecoveryCode)
                .where(
                    MfaRecoveryCode.user_id == user.id,
                    MfaRecoveryCode.code_hash == hash_token(normalize_recovery_code(code)),
                    MfaRecoveryCode.used_at.is_(None),
                )
                .with_for_update()
            )
            if recovery is not None:
                recovery.used_at = utcnow()
                method = "recovery_code"

        if method is None:
            audit(session, user.organization_id, user.id, MFA_FAILED, user.id)
        else:
            await mark_session_verified(session, principal)
            audit(
                session,
                user.organization_id,
                user.id,
                "auth.mfa.verified",
                user.id,
                {"method": method},
            )

    if method is None:
        raise HTTPException(status_code=400, detail="Invalid code")


async def reset(session: AsyncSession, username: str) -> User:
    """清除第二因素并吊销全部会话；仅供 CLI 在设备与恢复码都丢失时使用。"""
    async with session.begin():
        user = await session.scalar(
            select(User).where(User.username == username).with_for_update()
        )
        if user is None:
            raise LookupError(username)

        user.mfa_secret_encrypted = None
        user.mfa_enabled_at = None
        user.mfa_last_timecode = None
        await session.execute(delete(MfaRecoveryCode).where(MfaRecoveryCode.user_id == user.id))
        await session.execute(
            update(AuthSession)
            .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=utcnow())
        )
        audit(session, user.organization_id, None, "user.mfa.reset", user.id)
    return user
