"""AccessGrant 增删查（Wiki 页面与原始资料共用）。

调用方先按组织取得资源（管理授权不要求可阅读内容），在同一事务内调用这里的函数。
"""

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Principal
from app.models import AccessGrant, AuditEvent, OutboxEvent, User


def record_audit(
    session: AsyncSession,
    principal: Principal,
    resource_id: UUID,
    action: str,
    payload: dict,
) -> None:
    """只写审计、不写 Outbox（用于幂等重复动作的留痕）。"""
    session.add(
        AuditEvent(
            organization_id=principal.organization_id,
            actor_id=principal.user_id,
            action=action,
            resource_id=resource_id,
            payload=payload,
        )
    )


def record_event(
    session: AsyncSession,
    principal: Principal,
    resource_id: UUID,
    action: str,
    payload: dict,
) -> None:
    """审计与 Outbox 在调用方事务内一起写入（设计 12.1）。"""
    record_audit(session, principal, resource_id, action, payload)

    session.add(
        OutboxEvent(
            organization_id=principal.organization_id,
            event_type=action,
            payload=payload,
        )
    )


def add_creator_grant(
    session: AsyncSession,
    principal: Principal,
    resource_type: str,
    resource_id: UUID,
) -> None:
    # 创建者需要继续访问自己的受限资源
    session.add(
        AccessGrant(
            organization_id=principal.organization_id,
            resource_type=resource_type,
            resource_id=resource_id,
            user_id=principal.user_id,
            granted_by=principal.user_id,
        )
    )


async def list_grants(
    session: AsyncSession,
    resource_type: str,
    resource_id: UUID,
) -> list[AccessGrant]:
    # 不按组织过滤：调用方须先按组织校验资源归属（写路径 grant/revoke 另有组织条件做纵深防御）
    grants = await session.scalars(
        select(AccessGrant)
        .where(
            AccessGrant.resource_type == resource_type,
            AccessGrant.resource_id == resource_id,
        )
        .order_by(AccessGrant.created_at)
    )
    return list(grants)


async def grant(
    session: AsyncSession,
    principal: Principal,
    resource_type: str,
    resource_id: UUID,
    user_id: UUID,
    action: str,
    payload: dict,
) -> AccessGrant:
    grantee = await session.scalar(
        select(User).where(
            User.id == user_id,
            User.organization_id == principal.organization_id,
        )
    )
    if grantee is None:
        raise HTTPException(status_code=404, detail="User not found")

    existing = await session.scalar(
        select(AccessGrant).where(
            AccessGrant.organization_id == principal.organization_id,
            AccessGrant.resource_type == resource_type,
            AccessGrant.resource_id == resource_id,
            AccessGrant.user_id == user_id,
        )
    )
    if existing is not None:
        # 授权已存在：不新增记录，但写入审计，保证重复授权动作可追溯（幂等）
        record_audit(session, principal, resource_id, action, payload)
        return existing

    access_grant = AccessGrant(
        organization_id=principal.organization_id,
        resource_type=resource_type,
        resource_id=resource_id,
        user_id=user_id,
        granted_by=principal.user_id,
    )
    session.add(access_grant)
    record_event(session, principal, resource_id, action, payload)
    await session.flush()
    await session.refresh(access_grant)
    return access_grant


async def revoke(
    session: AsyncSession,
    principal: Principal,
    resource_type: str,
    resource_id: UUID,
    user_id: UUID,
    action: str,
    payload: dict,
) -> None:
    result = await session.execute(
        delete(AccessGrant).where(
            AccessGrant.organization_id == principal.organization_id,
            AccessGrant.resource_type == resource_type,
            AccessGrant.resource_id == resource_id,
            AccessGrant.user_id == user_id,
        )
    )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Grant not found")

    record_event(session, principal, resource_id, action, payload)
