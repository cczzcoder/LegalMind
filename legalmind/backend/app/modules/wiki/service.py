from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Principal
from app.models import AccessGrant, WikiPage, WikiRevision
from app.modules.authorization import grants
from app.modules.authorization.grants import record_event
from app.modules.authorization.service import AuthorizationService
from app.modules.wiki.schemas import CreatePage, CreateRevision


def record_change(
    session: AsyncSession,
    principal: Principal,
    page_id: UUID,
    revision: WikiRevision,
    action: str,
) -> None:
    record_event(
        session,
        principal,
        page_id,
        action,
        {
            "page_id": str(page_id),
            "revision_id": str(revision.id),
            "revision_number": revision.number,
        },
    )


async def get_visible_page(
    session: AsyncSession,
    principal: Principal,
    page_id: UUID,
    lock: bool = False,
) -> WikiPage:
    # 不存在与无权访问返回同样的 404，不泄露页面存在性
    statement = select(WikiPage).where(
        WikiPage.id == page_id,
        AuthorizationService.wiki_page_scope(principal),
    )
    if lock:
        statement = statement.with_for_update(of=WikiPage)

    page = await session.scalar(statement)
    if page is None:
        raise HTTPException(status_code=404, detail="Page not found")
    return page


async def get_org_page(
    session: AsyncSession,
    principal: Principal,
    page_id: UUID,
) -> WikiPage:
    """授权管理用：只按组织查找，管理授权不等于可阅读内容。"""
    page = await session.scalar(
        select(WikiPage)
        .where(
            WikiPage.id == page_id,
            WikiPage.organization_id == principal.organization_id,
        )
        .with_for_update()
    )
    if page is None:
        raise HTTPException(status_code=404, detail="Page not found")
    return page


async def create_page(
    session: AsyncSession,
    principal: Principal,
    data: CreatePage,
) -> WikiRevision:
    async with session.begin():
        page = WikiPage(
            organization_id=principal.organization_id,
            title=data.title,
            head_revision=1,
            access_scope=data.access_scope,
        )
        session.add(page)
        await session.flush()

        if data.access_scope == "restricted":
            grants.add_creator_grant(session, principal, "wiki_page", page.id)

        revision = WikiRevision(
            page_id=page.id,
            number=1,
            body=data.body,
            author_id=principal.user_id,
        )
        session.add(revision)
        await session.flush()

        record_change(
            session,
            principal,
            page.id,
            revision,
            "wiki.page.created",
        )

        await session.flush()
        await session.refresh(revision)

    return revision


async def create_revision(
    session: AsyncSession,
    principal: Principal,
    page_id: UUID,
    data: CreateRevision,
) -> WikiRevision:
    async with session.begin():
        page = await get_visible_page(session, principal, page_id, lock=True)

        if page.head_revision != data.expected_revision:
            raise HTTPException(
                status_code=409,
                detail="Revision conflict: reload before editing",
            )

        page.head_revision += 1

        revision = WikiRevision(
            page_id=page.id,
            number=page.head_revision,
            body=data.body,
            author_id=principal.user_id,
        )
        session.add(revision)
        await session.flush()

        record_change(
            session,
            principal,
            page.id,
            revision,
            "wiki.revision.created",
        )

        await session.flush()
        await session.refresh(revision)

    return revision


async def set_access_scope(
    session: AsyncSession,
    principal: Principal,
    page_id: UUID,
    access_scope: str,
) -> WikiPage:
    async with session.begin():
        page = await get_org_page(session, principal, page_id)
        before = page.access_scope
        page.access_scope = access_scope
        record_event(
            session,
            principal,
            page.id,
            "wiki.page.access_scope_changed",
            {"page_id": str(page.id), "before": before, "after": access_scope},
        )
        await session.flush()
        await session.refresh(page)

    return page


async def list_grants(
    session: AsyncSession,
    principal: Principal,
    page_id: UUID,
) -> list[AccessGrant]:
    async with session.begin():
        page = await get_org_page(session, principal, page_id)
        return await grants.list_grants(session, "wiki_page", page.id)


async def grant(
    session: AsyncSession,
    principal: Principal,
    page_id: UUID,
    user_id: UUID,
) -> AccessGrant:
    async with session.begin():
        page = await get_org_page(session, principal, page_id)
        return await grants.grant(
            session,
            principal,
            "wiki_page",
            page.id,
            user_id,
            "wiki.page.access_granted",
            {"page_id": str(page.id), "user_id": str(user_id)},
        )


async def revoke(
    session: AsyncSession,
    principal: Principal,
    page_id: UUID,
    user_id: UUID,
) -> None:
    async with session.begin():
        page = await get_org_page(session, principal, page_id)
        await grants.revoke(
            session,
            principal,
            "wiki_page",
            page.id,
            user_id,
            "wiki.page.access_revoked",
            {"page_id": str(page.id), "user_id": str(user_id)},
        )
