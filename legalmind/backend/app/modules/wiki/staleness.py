"""发布页面的失效检测与标记（设计 §10.2）。

「**来源更新先标记待复核，不自动删除历史说明**」——所以这里只算、只标记，**绝不动正文**。

两条失效信号，都是**可验证的事实**而不是猜测：

- **依据已被取代**：引用指向的法律版本 `legal_status` 变成 `repealed`。这个状态是落库时按
  §5.2 算出来的（新版本已生效时把公布更早的旧版本置 `repealed`），不是这里猜的。
- **引用正文变了**：引用条款的 `text_sha256` 与**发布时快照**不一致——同一版本被重新解析、
  或换了更优原件后重建过条款文本（§5.2 的多原件择优会重建条款文本）。**只比对版本号发现不了
  这种变化**，所以要留快照。

标记落在 `wiki_pages.review_due_at` / `review_due_reason`：这是**派生事实**，`flag_stale`
可重复执行、结果一致；**重新发布会清掉标记**——重新发布本身就是又复核过一遍。

审计只在**状态发生变化**时写：每次跑都写一条的话，审计日志会被例行扫描淹没。
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Principal
from app.models import (
    LegalInstrument,
    LegalVersion,
    ProvisionIdentity,
    ProvisionVersion,
    WikiPage,
    WikiRevision,
    WikiRevisionCitation,
)
from app.modules.authorization.grants import record_event

# 失效原因（也是审计里的取值）
SUPERSEDED = "superseded"
CHANGED = "changed"


@dataclass(frozen=True)
class StaleFinding:
    """一个待复核的页面。``reason`` 直接给人看，所以要写清是哪一条、为什么。"""

    page_id: UUID
    title: str
    revision_number: int
    reason: str


@dataclass(frozen=True)
class StaleReport:
    published: int
    stale: int
    newly_flagged: int
    cleared: int
    findings: tuple[StaleFinding, ...]


async def find_stale(session: AsyncSession) -> list[StaleFinding]:
    """列出依据已经失效的已发布页面。只读。"""
    rows = (
        await session.execute(
            select(
                WikiPage.id,
                WikiPage.title,
                WikiRevision.number,
                LegalVersion.legal_status,
                LegalInstrument.title,
                ProvisionIdentity.provision_number,
                ProvisionVersion.text_sha256,
                WikiRevisionCitation.provision_text_sha256,
            )
            # 只看**发布指针指向的**那版修订——草稿引用了什么与读者无关
            .join(
                WikiRevision,
                and_(
                    WikiRevision.page_id == WikiPage.id,
                    WikiRevision.number == WikiPage.published_revision,
                ),
            )
            .join(WikiRevisionCitation, WikiRevisionCitation.revision_id == WikiRevision.id)
            .join(
                ProvisionVersion, WikiRevisionCitation.provision_version_id == ProvisionVersion.id
            )
            .join(ProvisionIdentity, ProvisionVersion.provision_identity_id == ProvisionIdentity.id)
            .join(LegalVersion, ProvisionVersion.legal_version_id == LegalVersion.id)
            .join(LegalInstrument, LegalVersion.instrument_id == LegalInstrument.id)
        )
    ).all()

    reasons: dict[UUID, set[str]] = {}
    meta: dict[UUID, tuple[str, int]] = {}
    for (
        page_id,
        title,
        number,
        status,
        instrument,
        provision_number,
        current_sha,
        snapshot_sha,
    ) in rows:
        meta[page_id] = (title, number)
        where = f"{instrument}第{provision_number}条"
        if status == "repealed":
            reasons.setdefault(page_id, set()).add(f"{where} 所在版本已被取代")
        elif snapshot_sha is not None and snapshot_sha != current_sha:
            # 快照为空说明这条引用是在本功能之前发布的，没有可比对的基线——如实跳过，不猜
            reasons.setdefault(page_id, set()).add(f"{where} 正文已变更")

    return [
        StaleFinding(
            page_id=page_id,
            title=meta[page_id][0],
            revision_number=meta[page_id][1],
            reason="；".join(sorted(reasons[page_id])),
        )
        for page_id in sorted(reasons, key=str)
    ]


async def flag_stale(
    session: AsyncSession, principal: Principal, *, dry_run: bool = False
) -> StaleReport:
    """按检测结果打标 / 清标；**只在状态变化时**写审计。"""
    now = datetime.now(UTC)
    newly_flagged = 0
    cleared = 0

    # 检测查询必须在事务**内**跑：SELECT 会隐式开启事务，先查再 begin() 会撞
    # 「A transaction is already begun on this Session」
    async with session.begin():
        findings = await find_stale(session)
        by_page = {item.page_id: item for item in findings}
        pages = list(
            await session.scalars(select(WikiPage).where(WikiPage.published_revision.is_not(None)))
        )
        for page in pages:
            finding = by_page.get(page.id)
            if finding is not None and page.review_due_at is None:
                newly_flagged += 1
                if not dry_run:
                    page.review_due_at = now
                    page.review_due_reason = finding.reason
                    record_event(
                        session,
                        principal,
                        page.id,
                        "wiki.page.review_due",
                        {"page_id": str(page.id), "reason": finding.reason},
                    )
            elif finding is None and page.review_due_at is not None:
                cleared += 1
                if not dry_run:
                    page.review_due_at = None
                    page.review_due_reason = None
                    record_event(
                        session,
                        principal,
                        page.id,
                        "wiki.page.review_due_cleared",
                        {"page_id": str(page.id)},
                    )
        if not dry_run:
            await session.flush()

    return StaleReport(
        published=len(pages),
        stale=len(findings),
        newly_flagged=newly_flagged,
        cleared=cleared,
        findings=tuple(findings),
    )
