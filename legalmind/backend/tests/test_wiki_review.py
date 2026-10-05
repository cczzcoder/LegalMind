"""Wiki 审核发布（设计 §10.2、§10.3）：需要 TEST_DATABASE_URL。

覆盖状态机、引用检查、独立审核，以及 §10.3 的权限继承（引用了受限原件的页面，看不到原件的人
也看不到这个页面）。
"""

from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.adapters.storage import LocalFileStorage, get_storage
from app.core.security import Principal, hash_password
from app.models import (
    AuditEvent,
    LegalInstrument,
    LegalVersion,
    ProvisionIdentity,
    ProvisionVersion,
    SourceArtifact,
    User,
    WikiPage,
    WikiRevision,
    WikiRevisionCitation,
)
from app.modules.authorization.service import (
    AUDIT_READ,
    DOCUMENT_DOWNLOAD,
    REVIEW_DECIDE,
    WIKI_WRITE,
)
from app.modules.authorization.service import AuthorizationService as Auth
from app.modules.parsing.service import parse_artifact
from app.modules.wiki import service, staleness
from app.modules.wiki.schemas import CreatePage, CreateRevision
from tests.helpers import PASSWORD, import_document_for_parsing

pytestmark = pytest.mark.anyio


@pytest.fixture
def storage(tmp_path, make_client):
    from app.main import app

    storage = LocalFileStorage(tmp_path)
    app.dependency_overrides[get_storage] = lambda: storage
    return storage


@pytest.fixture
def actors(session_factory, make_user):
    """**同一个组织**下的多个 Principal。

    页面可见性按组织过滤（`wiki_page_scope`），而 `make_user` 每次都建新组织——审核流是
    作者与审核员之间的交互，跨组织根本测不了。
    """
    state: dict = {"organization_id": None}

    async def build(*roles: str) -> Principal:
        if state["organization_id"] is None:
            state["organization_id"] = (await make_user("reader")).organization_id
        async with session_factory() as session, session.begin():
            user = User(
                organization_id=state["organization_id"],
                username=f"u-{uuid4().hex[:12]}",
                password_hash=hash_password(PASSWORD),
                is_active=True,
            )
            session.add(user)
            await session.flush()
            return Principal(
                organization_id=state["organization_id"],
                user_id=user.id,
                roles=frozenset(roles),
            )

    return build


async def _provision(session_factory, make_client, make_user, storage) -> UUID:
    """走真实导入与解析，拿到一个可以引用的条款版本。"""
    name = f"示例测试法（{uuid4().hex[:8]}）"
    content = (
        f"{name}\n（2026年5月1日第十四届全国人民代表大会常务委员会第一次会议通过）\n"
        "第一条　为了测试，制定本法。\n"
    ).encode()
    editor, document_id = await import_document_for_parsing(
        make_client, make_user, content, f"{name}.txt"
    )
    async with session_factory() as session:
        await parse_artifact(
            session,
            storage,
            Principal(organization_id=editor.organization_id, user_id=editor.id, roles=frozenset()),
            document_id,
        )
    async with session_factory() as session:
        return await session.scalar(
            select(ProvisionVersion.id)
            .join(ProvisionIdentity, ProvisionVersion.provision_identity_id == ProvisionIdentity.id)
            .join(LegalVersion, ProvisionVersion.legal_version_id == LegalVersion.id)
            .join(LegalInstrument, LegalVersion.instrument_id == LegalInstrument.id)
            .where(LegalInstrument.title == name)
        )


async def _restrict(session_factory, provision: UUID) -> None:
    """把条款所在的原件改成受限——查询与更新必须在**同一个会话**里，否则改的是已关闭会话上的对象。"""
    async with session_factory() as session, session.begin():
        artifact = await session.scalar(
            select(SourceArtifact)
            .join(LegalVersion, LegalVersion.artifact_id == SourceArtifact.id)
            .join(ProvisionVersion, ProvisionVersion.legal_version_id == LegalVersion.id)
            .where(ProvisionVersion.id == provision)
        )
        artifact.access_scope = "restricted"


async def _draft(session_factory, author: Principal, *, body: str = "初稿正文") -> tuple:
    """建页面 + 第 1 号草稿修订。"""
    async with session_factory() as session:
        revision = await service.create_page(
            session, author, CreatePage(title=f"页面-{uuid4().hex[:8]}", body=body)
        )
    return revision.page_id, revision.number


async def _submit_with_citation(session_factory, author, page_id, number, provision) -> None:
    async with session_factory() as session:
        await service.set_citations(session, author, page_id, number, [provision])
        await service.submit_revision(session, author, page_id, number)


async def _publish(session_factory, reviewer: Principal, page_id, number, note=None):
    async with session_factory() as session:
        return await service.publish_revision(session, reviewer, page_id, number, note)


async def test_submit_locks_the_revision(make_client, make_user, actors, storage, session_factory):
    """§10.2「提交后锁定该修订」——提交后引用也不能再改，要改就新开修订。"""
    author = await actors("editor")
    page_id, number = await _draft(session_factory, author)
    provision = await _provision(session_factory, make_client, make_user, storage)
    await _submit_with_citation(session_factory, author, page_id, number, provision)

    async with session_factory() as session:
        with pytest.raises(HTTPException) as caught:
            await service.set_citations(session, author, page_id, number, [provision])
    assert caught.value.status_code == 409


async def test_publish_requires_at_least_one_citation(
    make_client, make_user, actors, storage, session_factory
):
    """法律结论没有原文依据就不该发布（§10.2 引用检查）。"""
    author = await actors("editor")
    reviewer = await actors("legal_reviewer")
    page_id, number = await _draft(session_factory, author)
    async with session_factory() as session:
        await service.submit_revision(session, author, page_id, number)

    with pytest.raises(HTTPException) as caught:
        await _publish(session_factory, reviewer, page_id, number)
    assert caught.value.status_code == 422


async def test_author_cannot_review_their_own_revision(
    make_client, make_user, actors, storage, session_factory
):
    """§10.3「独立审核」——自己批自己等于没审。"""
    author = await actors("editor", "legal_reviewer")
    page_id, number = await _draft(session_factory, author)
    provision = await _provision(session_factory, make_client, make_user, storage)
    await _submit_with_citation(session_factory, author, page_id, number, provision)

    with pytest.raises(HTTPException) as caught:
        await _publish(session_factory, author, page_id, number)
    assert caught.value.status_code == 403


async def test_publish_requires_the_reviewer_to_reach_every_cited_source(
    make_client, make_user, actors, storage, session_factory
):
    """审核人看不到引用原件就不能批——否则「公开化」没人负责（§10.3）。"""
    author = await actors("editor")
    reviewer = await actors("legal_reviewer")
    page_id, number = await _draft(session_factory, author)
    provision = await _provision(session_factory, make_client, make_user, storage)
    await _submit_with_citation(session_factory, author, page_id, number, provision)
    await _restrict(session_factory, provision)

    with pytest.raises(HTTPException) as caught:
        await _publish(session_factory, reviewer, page_id, number)
    assert caught.value.status_code == 403


async def test_publish_sets_the_pointer_and_readers_see_that_revision(
    make_client, make_user, actors, storage, session_factory
):
    """读者看到的是发布指针指向的修订，不是最新修订（§10.2）。"""
    author = await actors("editor")
    reviewer = await actors("legal_reviewer")
    page_id, number = await _draft(session_factory, author, body="第一版")
    provision = await _provision(session_factory, make_client, make_user, storage)
    await _submit_with_citation(session_factory, author, page_id, number, provision)
    published = await _publish(session_factory, reviewer, page_id, number, "核对无误")
    assert published.status == "published"
    assert published.reviewed_by == reviewer.user_id
    assert published.review_note == "核对无误"

    # 再开一版草稿：发布指针不该跟着动
    async with session_factory() as session:
        await service.create_revision(
            session, author, page_id, CreateRevision(expected_revision=1, body="第二版")
        )
    async with session_factory() as session:
        visible = await service.get_published_revision(session, author, page_id)
    assert visible.number == 1
    assert visible.body == "第一版"


async def test_rejected_revision_can_be_resubmitted_after_fixing_citations(
    make_client, make_user, actors, storage, session_factory
):
    """驳回后能改引用再提交——引用检查失败是驳回的常见原因，逼人新开修订没有意义。"""
    author = await actors("editor")
    reviewer = await actors("legal_reviewer")
    page_id, number = await _draft(session_factory, author)
    provision = await _provision(session_factory, make_client, make_user, storage)
    async with session_factory() as session:
        await service.submit_revision(session, author, page_id, number)
        rejected = await service.reject_revision(session, reviewer, page_id, number, "缺依据")
    assert rejected.status == "rejected"

    await _submit_with_citation(session_factory, author, page_id, number, provision)
    assert (await _publish(session_factory, reviewer, page_id, number)).status == "published"


async def test_page_citing_an_inaccessible_source_is_hidden_from_readers(
    make_client, make_user, actors, storage, session_factory
):
    """§10.3 权限继承：引用受限原件的页面，看不到原件的人也看不到页面。

    「不能因为内容被概括或改写就自动取消原文限制」——判据是**引用**。
    """
    author = await actors("editor")
    reviewer = await actors("legal_reviewer")
    outsider = await actors("reader")
    page_id, number = await _draft(session_factory, author)
    provision = await _provision(session_factory, make_client, make_user, storage)
    await _submit_with_citation(session_factory, author, page_id, number, provision)
    await _publish(session_factory, reviewer, page_id, number)

    async with session_factory() as session:
        await service.get_visible_page(session, outsider, page_id)  # 原件公开，看得见

    await _restrict(session_factory, provision)

    async with session_factory() as session:
        with pytest.raises(HTTPException) as caught:
            await service.get_visible_page(session, outsider, page_id)
    assert caught.value.status_code == 404


async def test_pending_queue_lists_submitted_revisions(
    make_client, make_user, actors, storage, session_factory
):
    author = await actors("editor")
    reviewer = await actors("legal_reviewer")
    page_id, number = await _draft(session_factory, author)
    async with session_factory() as session:
        await service.submit_revision(session, author, page_id, number)
        pending = await service.list_pending(session, reviewer)
    assert [(item.page_id, item.number) for item in pending] == [(page_id, number)]


async def test_unknown_revision_number_is_a_404(session_factory, actors):
    author = await actors("editor")
    page_id, _number = await _draft(session_factory, author)
    async with session_factory() as session:
        with pytest.raises(HTTPException) as caught:
            await service.submit_revision(session, author, page_id, 99)
    assert caught.value.status_code == 404


async def test_revision_status_constraint_rejects_unknown_values(
    make_client, make_user, actors, storage, session_factory
):
    """状态取值由 ck_wiki_revision_status 兜住——状态机错了不该悄悄落库。"""
    author = await actors("editor")
    page_id, number = await _draft(session_factory, author)
    with pytest.raises(IntegrityError):
        async with session_factory() as session, session.begin():
            revision = await session.scalar(
                select(WikiRevision).where(
                    WikiRevision.page_id == page_id, WikiRevision.number == number
                )
            )
            revision.status = "not-a-status"


def test_review_and_audit_permissions_are_assigned():
    """需求第 3 节：审核员要能决定、审计员要能看记录；两者都不该拿到写权限。"""
    reviewer = Principal(
        organization_id=uuid4(), user_id=uuid4(), roles=frozenset({"legal_reviewer"})
    )
    auditor = Principal(organization_id=uuid4(), user_id=uuid4(), roles=frozenset({"auditor"}))
    assert Auth.can(reviewer, REVIEW_DECIDE)
    assert Auth.can(auditor, AUDIT_READ)
    # 审计是只读的，且不含原件下载
    assert not Auth.can(auditor, DOCUMENT_DOWNLOAD)
    assert not Auth.can(auditor, WIKI_WRITE)
    assert not Auth.can(reviewer, WIKI_WRITE)


async def _published_page(session_factory, make_client, make_user, storage, actors) -> tuple:
    """发布一个引用了真实条款的页面，返回 (page_id, provision_version_id, author, reviewer)。"""
    author = await actors("editor")
    reviewer = await actors("legal_reviewer")
    page_id, number = await _draft(session_factory, author)
    provision = await _provision(session_factory, make_client, make_user, storage)
    await _submit_with_citation(session_factory, author, page_id, number, provision)
    await _publish(session_factory, reviewer, page_id, number)
    return page_id, provision, author, reviewer


async def _flag(session_factory, reviewer, *, dry_run=False):
    async with session_factory() as session:
        return await staleness.flag_stale(session, reviewer, dry_run=dry_run)


async def _rewrite_provision_text(session_factory, provision: UUID) -> None:
    """模拟「同一版本被重新解析、或换了更优原件后重建条款文本」。"""
    async with session_factory() as session, session.begin():
        row = await session.scalar(select(ProvisionVersion).where(ProvisionVersion.id == provision))
        row.text = "第一条　测试条文（重新解析后改了字）。"
        row.text_sha256 = "f" * 64


async def _review_due(session_factory, page_id: UUID):
    async with session_factory() as session:
        page = await session.scalar(select(WikiPage).where(WikiPage.id == page_id))
    return page.review_due_at, page.review_due_reason


def _findings_for(report, page_id: UUID) -> list:
    """只看**自己那一页**的结论。

    测试库是共享且跨运行累积的，`flag_stale` 又会扫全库——拿全局计数做断言必然被别的用例污染。
    """
    return [item for item in report.findings if item.page_id == page_id]


async def test_publishing_snapshots_the_cited_text_hash(
    make_client, make_user, actors, storage, session_factory
):
    """§10.2：要能判断「来源更新了」，得先记住审核当时看到的是哪段文字。"""
    _page_id, provision, _author, _reviewer = await _published_page(
        session_factory, make_client, make_user, storage, actors
    )
    async with session_factory() as session:
        snapshot = await session.scalar(
            select(WikiRevisionCitation.provision_text_sha256)
            .join(WikiRevision, WikiRevisionCitation.revision_id == WikiRevision.id)
            .where(WikiRevisionCitation.provision_version_id == provision)
        )
    assert snapshot is not None and snapshot != "f" * 64


async def test_changed_provision_text_marks_the_page_for_review(
    make_client, make_user, actors, storage, session_factory
):
    """正文变了就标记——**只标记，不动正文**（§10.2 保留历史说明）。"""
    page_id, provision, _author, reviewer = await _published_page(
        session_factory, make_client, make_user, storage, actors
    )
    assert (await _review_due(session_factory, page_id))[0] is None

    await _rewrite_provision_text(session_factory, provision)

    report = await _flag(session_factory, reviewer)
    mine = _findings_for(report, page_id)
    assert len(mine) == 1
    assert "正文已变更" in mine[0].reason

    due_at, reason = await _review_due(session_factory, page_id)
    assert due_at is not None and "正文已变更" in reason
    # 正文一个字都没改
    async with session_factory() as session:
        revision = await session.scalar(
            select(WikiRevision).where(WikiRevision.page_id == page_id, WikiRevision.number == 1)
        )
    assert revision.body == "初稿正文"


async def test_superseded_legal_version_marks_the_page_for_review(
    make_client, make_user, actors, storage, session_factory
):
    """引用的法律版本被取代（`legal_status` 变 repealed）就标记——这是落库时算出的事实。"""
    page_id, provision, _author, reviewer = await _published_page(
        session_factory, make_client, make_user, storage, actors
    )
    async with session_factory() as session, session.begin():
        version = await session.scalar(
            select(LegalVersion)
            .join(ProvisionVersion, ProvisionVersion.legal_version_id == LegalVersion.id)
            .where(ProvisionVersion.id == provision)
        )
        version.legal_status = "repealed"

    report = await _flag(session_factory, reviewer)
    mine = _findings_for(report, page_id)
    assert len(mine) == 1
    assert "已被取代" in mine[0].reason


async def test_republishing_clears_the_review_mark(
    make_client, make_user, actors, storage, session_factory
):
    """重新发布本身就是又复核过一遍，标记随之清掉（§10.2）。"""
    page_id, provision, author, reviewer = await _published_page(
        session_factory, make_client, make_user, storage, actors
    )
    await _rewrite_provision_text(session_factory, provision)
    await _flag(session_factory, reviewer)
    assert (await _review_due(session_factory, page_id))[0] is not None

    async with session_factory() as session:
        await service.create_revision(
            session, author, page_id, CreateRevision(expected_revision=1, body="复核后重写")
        )
    await _submit_with_citation(session_factory, author, page_id, 2, provision)
    await _publish(session_factory, reviewer, page_id, 2)

    due_at, reason = await _review_due(session_factory, page_id)
    assert due_at is None and reason is None


async def _review_due_events(session_factory, page_id: UUID) -> int:
    async with session_factory() as session:
        return await session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.action == "wiki.page.review_due",
                AuditEvent.resource_id == page_id,
            )
        )


async def test_flag_stale_is_idempotent_and_audits_only_on_change(
    make_client, make_user, actors, storage, session_factory
):
    """例行扫描每次都写审计会把日志淹掉——只在状态变化时写。"""
    page_id, provision, _author, reviewer = await _published_page(
        session_factory, make_client, make_user, storage, actors
    )
    await _rewrite_provision_text(session_factory, provision)

    await _flag(session_factory, reviewer)
    first = await _review_due_events(session_factory, page_id)
    assert first == 1

    await _flag(session_factory, reviewer)
    assert await _review_due_events(session_factory, page_id) == first


async def test_flag_stale_dry_run_changes_nothing(
    make_client, make_user, actors, storage, session_factory
):
    page_id, provision, _author, reviewer = await _published_page(
        session_factory, make_client, make_user, storage, actors
    )
    await _rewrite_provision_text(session_factory, provision)

    report = await _flag(session_factory, reviewer, dry_run=True)
    assert len(_findings_for(report, page_id)) == 1
    assert (await _review_due(session_factory, page_id))[0] is None
    assert await _review_due_events(session_factory, page_id) == 0
