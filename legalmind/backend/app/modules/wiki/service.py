from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Principal
from app.models import (
    AccessGrant,
    LegalVersion,
    ProvisionVersion,
    SourceArtifact,
    WikiPage,
    WikiRevision,
    WikiRevisionCitation,
)
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


async def _revision_at(
    session: AsyncSession, page: WikiPage, number: int, *, allowed: tuple[str, ...]
) -> WikiRevision:
    revision = await session.scalar(
        select(WikiRevision).where(
            WikiRevision.page_id == page.id,
            WikiRevision.number == number,
        )
    )
    if revision is None:
        raise HTTPException(status_code=404, detail="Revision not found")
    if revision.status not in allowed:
        # 状态不对就说清当前状态，别让人猜为什么改不动（§10.2 的状态机）
        raise HTTPException(
            status_code=409,
            detail=f"Revision {number} is {revision.status}, expected one of {list(allowed)}",
        )
    return revision


async def set_citations(
    session: AsyncSession,
    principal: Principal,
    page_id: UUID,
    revision_number: int,
    provision_version_ids: list[UUID],
) -> list[UUID]:
    """为**尚未提交**的修订登记引用（覆盖式）。引用绑的是具体条款版本（§5.3）。"""
    async with session.begin():
        page = await get_visible_page(session, principal, page_id, lock=True)
        revision = await _revision_at(session, page, revision_number, allowed=("draft", "rejected"))
        await session.execute(
            delete(WikiRevisionCitation).where(WikiRevisionCitation.revision_id == revision.id)
        )
        unique = list(dict.fromkeys(provision_version_ids))
        for identifier in unique:
            session.add(
                WikiRevisionCitation(revision_id=revision.id, provision_version_id=identifier)
            )
        record_event(
            session,
            principal,
            page.id,
            "wiki.revision.citations_set",
            {
                "page_id": str(page.id),
                "revision_number": revision.number,
                "count": len(unique),
            },
        )
        await session.flush()

    return unique


async def submit_revision(
    session: AsyncSession,
    principal: Principal,
    page_id: UUID,
    revision_number: int,
) -> WikiRevision:
    """提交审核：锁定该修订（§10.2）。此后改正文要新开修订，只有**引用**还能补。"""
    async with session.begin():
        page = await get_visible_page(session, principal, page_id, lock=True)
        revision = await _revision_at(session, page, revision_number, allowed=("draft", "rejected"))
        revision.status = "submitted"
        record_change(session, principal, page.id, revision, "wiki.revision.submitted")
        await session.flush()
        await session.refresh(revision)

    return revision


async def _check_citations(session: AsyncSession, principal: Principal, revision: WikiRevision):
    """发布前的引用检查（§10.2）。两条：

    - **至少一条引用**：法律结论没有原文依据就不该发布；
    - **审核人对每条引用都有访问权**：§10.3「不能因为内容被概括或改写就自动取消原文限制」
      ——审查看不到原件，就没法对公开化负责。

    引用指向的条款版本是否存在由外键保证（``ondelete=RESTRICT``，删不掉）。
    """
    cited = list(
        await session.scalars(
            select(WikiRevisionCitation.provision_version_id).where(
                WikiRevisionCitation.revision_id == revision.id
            )
        )
    )
    if not cited:
        raise HTTPException(
            status_code=422,
            detail="Revision cites no provision; a legal conclusion needs原文依据 before publishing",
        )
    accessible = set(
        await session.scalars(
            select(ProvisionVersion.id)
            .join(LegalVersion, ProvisionVersion.legal_version_id == LegalVersion.id)
            .join(SourceArtifact, LegalVersion.artifact_id == SourceArtifact.id)
            .where(
                ProvisionVersion.id.in_(cited),
                AuthorizationService.document_scope(principal),
            )
        )
    )
    inaccessible = [identifier for identifier in cited if identifier not in accessible]
    if inaccessible:
        raise HTTPException(
            status_code=403,
            detail=(
                f"{len(inaccessible)} of {len(cited)} cited provisions are not accessible to "
                "the reviewer; 公开化必须由能看到全部依据的人审核"
            ),
        )


async def publish_revision(
    session: AsyncSession,
    principal: Principal,
    page_id: UUID,
    revision_number: int,
    note: str | None,
) -> WikiRevision:
    """审核通过并发布：引用检查通过才写发布指针（§10.2）。"""
    async with session.begin():
        # 用可见范围取页面：审核人看不到页面就审不了，这是 §10.3 的必然结果
        page = await get_visible_page(session, principal, page_id, lock=True)
        revision = await _revision_at(session, page, revision_number, allowed=("submitted",))
        if revision.author_id == principal.user_id:
            # §10.3「独立审核」：作者不能审自己的修订
            raise HTTPException(status_code=403, detail="Reviewer must not be the author")

        await _check_citations(session, principal, revision)

        revision.status = "published"
        revision.reviewed_by = principal.user_id
        revision.reviewed_at = datetime.now(UTC)
        revision.review_note = note
        page.published_revision = revision.number
        # 记下「审核当时看到的是哪段文字」（§10.2）——之后同一版本被重新解析、或换了更优原件
        # 重建过条款文本，就要能发现；只比对版本号发现不了正文变化
        await session.execute(
            update(WikiRevisionCitation)
            .where(WikiRevisionCitation.revision_id == revision.id)
            .values(
                provision_text_sha256=(
                    select(ProvisionVersion.text_sha256)
                    .where(ProvisionVersion.id == WikiRevisionCitation.provision_version_id)
                    .scalar_subquery()
                )
            )
        )
        # 重新发布本身就是又复核过一遍，待复核标记随之清掉（§10.2）
        page.review_due_at = None
        page.review_due_reason = None
        record_event(
            session,
            principal,
            page.id,
            "wiki.revision.published",
            {
                "page_id": str(page.id),
                "revision_number": revision.number,
                "author_id": str(revision.author_id),
                "note": note,
            },
        )
        await session.flush()
        await session.refresh(revision)

    return revision


async def reject_revision(
    session: AsyncSession,
    principal: Principal,
    page_id: UUID,
    revision_number: int,
    note: str | None,
) -> WikiRevision:
    """驳回：修订回到作者手里。**可以改引用后重新提交**——引用检查失败是驳回的常见原因，
    逼作者为了补一条引用而新开修订没有意义。"""
    async with session.begin():
        page = await get_visible_page(session, principal, page_id, lock=True)
        revision = await _revision_at(session, page, revision_number, allowed=("submitted",))
        revision.status = "rejected"
        revision.reviewed_by = principal.user_id
        revision.reviewed_at = datetime.now(UTC)
        revision.review_note = note
        record_event(
            session,
            principal,
            page.id,
            "wiki.revision.rejected",
            {
                "page_id": str(page.id),
                "revision_number": revision.number,
                "note": note,
            },
        )
        await session.flush()
        await session.refresh(revision)

    return revision


async def list_pending(
    session: AsyncSession, principal: Principal
) -> list[tuple[WikiRevision, str]]:
    """待审队列——此前只能靠 SQL 查（README 记的缺口）。

    返回 `(revision, page_title)`：**审核人要看到的是「哪个页面等着审」**，只给 `page_id`
    等于让人拿 UUID 去别处对——队列本身就失去意义了。标题本来就在 join 里，顺手带出来。
    """
    async with session.begin():
        rows = await session.execute(
            select(WikiRevision, WikiPage.title)
            .join(WikiPage, WikiRevision.page_id == WikiPage.id)
            .where(
                WikiRevision.status == "submitted",
                AuthorizationService.wiki_page_scope(principal),
            )
            .order_by(WikiRevision.created_at, WikiRevision.id)
        )
        return list(rows.all())


async def get_published_revision(
    session: AsyncSession, principal: Principal, page_id: UUID
) -> WikiRevision:
    """读者看到的是**发布指针指向的修订**，不是最新修订（§10.2）。"""
    async with session.begin():
        page = await get_visible_page(session, principal, page_id)
        if page.published_revision is None:
            raise HTTPException(status_code=404, detail="Page has no published revision")
        revision = await session.scalar(
            select(WikiRevision).where(
                WikiRevision.page_id == page.id,
                WikiRevision.number == page.published_revision,
            )
        )
        if revision is None:  # pragma: no cover - 受 ck_wiki_page_published_within_head 保护
            raise HTTPException(status_code=404, detail="Published revision not found")
        return revision


async def list_citations(
    session: AsyncSession, principal: Principal, page_id: UUID, revision_number: int
) -> list[WikiRevisionCitation]:
    async with session.begin():
        page = await get_visible_page(session, principal, page_id)
        revision = await _revision_at(
            session, page, revision_number, allowed=("draft", "submitted", "published", "rejected")
        )
        return list(
            await session.scalars(
                select(WikiRevisionCitation)
                .where(WikiRevisionCitation.revision_id == revision.id)
                .order_by(WikiRevisionCitation.created_at)
            )
        )
