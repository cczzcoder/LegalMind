"""撤下原件集成测试（设计 §15.3）：需要 TEST_DATABASE_URL。

覆盖：撤下后原件、解析版本、分块、定位、脱敏映射全部删除且文件移除；同一版本还有别的合法来源
原件时版本保留并从该原件重建；**整批撤下**不会让待撤下的原件互相「重建」；dry-run 只输出计划。
"""

from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.adapters.storage import LocalFileStorage, get_storage
from app.core.security import Principal
from app.models import (
    AuditEvent,
    Chunk,
    ChunkSpan,
    Job,
    LegalInstrument,
    LegalVersion,
    ParseRevision,
    ProvisionIdentity,
    ProvisionVersion,
    RedactionEntity,
    SourceArtifact,
)
from app.modules.documents.withdrawal import withdraw_documents
from app.modules.parsing.service import parse_artifact
from tests.helpers import import_document_for_parsing

pytestmark = pytest.mark.anyio

REASON = "来源未取得授权（FR-01、设计 §20.3）"


@pytest.fixture
def storage(tmp_path, make_client):
    from app.main import app

    storage = LocalFileStorage(tmp_path)
    app.dependency_overrides[get_storage] = lambda: storage
    return storage


def _principal(user) -> Principal:
    return Principal(organization_id=user.organization_id, user_id=user.id, roles=frozenset())


def law_name() -> str:
    return f"示例测试法（{uuid4().hex[:8]}）"


def law_text(
    name: str,
    *,
    effective: str = "",
    phone: str | None = None,
    passed: str = "2026年5月1日",
    extra_article: bool = False,
) -> bytes:
    clause = f"　{effective}" if effective else ""
    body = "第一条　为了测试，制定本法。"
    if phone:
        body += f"\n第二条　联系方式为{phone}，请依法处理。"
    if extra_article:
        body += "\n第三条　仅为该版本存在的条文。"
    return (
        f"{name}\n（{passed}第十四届全国人民代表大会常务委员会第一次会议通过{clause}）\n{body}\n"
    ).encode()


async def _parse(make_client, make_user, storage, session_factory, content, filename):
    editor, document_id = await import_document_for_parsing(
        make_client, make_user, content, filename
    )
    async with session_factory() as session:
        await parse_artifact(session, storage, _principal(editor), document_id)
    return editor, document_id


async def test_withdraw_removes_artifact_and_derivatives(
    make_client, make_user, storage, session_factory
):
    name = law_name()
    editor, document_id = await _parse(
        make_client,
        make_user,
        storage,
        session_factory,
        law_text(name, phone="13800138000"),
        f"{name}.txt",
    )

    async with session_factory() as session:
        object_key = (await session.get(SourceArtifact, document_id)).object_key
        revision_ids = list(
            await session.scalars(
                select(ParseRevision.id).where(ParseRevision.artifact_id == document_id)
            )
        )
        chunk_ids = list(
            await session.scalars(select(Chunk.id).where(Chunk.parse_revision_id.in_(revision_ids)))
        )
        assert revision_ids and chunk_ids
        assert (
            await session.scalar(
                select(func.count(RedactionEntity.id)).where(
                    RedactionEntity.parse_revision_id.in_(revision_ids)
                )
            )
            == 1
        )

    async with session_factory() as session:
        report = await withdraw_documents(
            session, _principal(editor), storage, [document_id], reason=REASON
        )

    assert len(report.documents) == 1
    assert report.documents[0].parse_revisions == len(revision_ids)
    assert report.documents[0].chunks == len(chunk_ids)
    assert report.documents[0].redaction_entities == 1
    assert report.detach_versions == ("2026年",)
    # 没有别的合法来源原件：版本与本体一并删除
    assert report.remove_versions == ("2026年",)
    assert report.remove_instruments == (name,)
    assert report.relink_from == ()

    async with session_factory() as session:
        assert await session.get(SourceArtifact, document_id) is None
        assert (
            await session.scalar(
                select(func.count(ParseRevision.id)).where(ParseRevision.id.in_(revision_ids))
            )
            == 0
        )
        assert (
            await session.scalar(select(func.count(Chunk.id)).where(Chunk.id.in_(chunk_ids))) == 0
        )
        assert (
            await session.scalar(
                select(func.count(ChunkSpan.id)).where(ChunkSpan.chunk_id.in_(chunk_ids))
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count(RedactionEntity.id)).where(
                    RedactionEntity.parse_revision_id.in_(revision_ids)
                )
            )
            == 0
        )
        assert (
            await session.scalar(select(LegalInstrument).where(LegalInstrument.title == name))
            is None
        )
        withdrawn = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "document.withdrawn",
                AuditEvent.resource_id == document_id,
            )
        )
        assert withdrawn.payload["reason"] == REASON
        # 记下对象键，文件删除失败时可据此手工清理
        assert withdrawn.payload["object_key"] == object_key

    # 原件字节也必须移除（§15.3 要求删除覆盖原件）
    with pytest.raises(FileNotFoundError):
        storage.open(object_key)


async def test_withdraw_keeps_version_backed_by_another_artifact(
    make_client, make_user, storage, session_factory
):
    """同一版本还有别的合法来源原件时，撤下一份后版本保留并从存活原件重建（实测商标法场景）。"""
    name = law_name()
    # 甲：带施行日期 → 效力状态确定、排序更优，成为该版本原件
    editor_a, document_a = await _parse(
        make_client,
        make_user,
        storage,
        session_factory,
        law_text(name, effective="自2027年1月1日起施行"),
        f"{name}.txt",
    )
    _editor_b, document_b = await _parse(
        make_client, make_user, storage, session_factory, law_text(name), f"{name}.txt"
    )

    async with session_factory() as session:
        instrument = await session.scalar(
            select(LegalInstrument).where(LegalInstrument.title == name)
        )
        version = await session.scalar(
            select(LegalVersion).where(LegalVersion.instrument_id == instrument.id)
        )
        assert version.artifact_id == document_a
        assert version.legal_status == "not_yet_effective"

    async with session_factory() as session:
        report = await withdraw_documents(
            session, _principal(editor_a), storage, [document_a], reason=REASON
        )

    assert report.remove_versions == ()
    assert report.remove_instruments == ()
    assert report.relink_from == (f"{name}.txt",)

    async with session_factory() as session:
        instrument = await session.scalar(
            select(LegalInstrument).where(LegalInstrument.title == name)
        )
        assert instrument is not None
        version = await session.scalar(
            select(LegalVersion).where(LegalVersion.instrument_id == instrument.id)
        )
        # 版本改由存活原件承载，条款文本从它重建
        assert version.artifact_id == document_b
        provisions = list(
            await session.scalars(
                select(ProvisionVersion).where(ProvisionVersion.legal_version_id == version.id)
            )
        )
        assert len(provisions) == 1
        # 效力状态原本来自被撤下的原件，撤下后必须回落到存活原件能支撑的结论
        assert version.legal_status == "unknown"
        assert version.effective_from is None


async def test_batch_withdraw_does_not_relink_from_other_withdrawn_artifacts(
    make_client, make_user, storage, session_factory
):
    """整批撤下时，待撤下的原件不能互为「存活原件」——否则会凭空重建即将删除的版本。

    实测宪法：2004 修正 PDF 与 2018 修正 PDF 同属一个本体，逐份撤下会互相重建。
    """
    name = law_name()
    editor_a, document_a = await _parse(
        make_client, make_user, storage, session_factory, law_text(name), f"{name}.txt"
    )
    # 乙：同版本但带施行日期 → 排序更优，成为该版本原件
    _editor_b, document_b = await _parse(
        make_client,
        make_user,
        storage,
        session_factory,
        law_text(name, effective="自2027年1月1日起施行"),
        f"{name}.txt",
    )
    # 丙：同本体的**另一个**版本，撤下乙丙后应只剩甲的版本
    _editor_c, document_c = await _parse(
        make_client,
        make_user,
        storage,
        session_factory,
        law_text(name, effective="自2025年6月1日起施行", passed="2025年1月1日"),
        f"{name}.txt",
    )

    async with session_factory() as session:
        report = await withdraw_documents(
            session, _principal(editor_a), storage, [document_b, document_c], reason=REASON
        )

    # 乙、丙被整批排除，只有甲是存活原件
    assert report.relink_from == (f"{name}.txt",)
    assert report.detach_versions == ("2025年", "2026年")
    # 丙的版本没有存活原件支撑 → 删除；甲、乙的版本由甲重建 → 保留
    assert report.remove_versions == ("2025年",)
    assert report.remove_instruments == ()

    async with session_factory() as session:
        instrument = await session.scalar(
            select(LegalInstrument).where(LegalInstrument.title == name)
        )
        versions = list(
            await session.scalars(
                select(LegalVersion).where(LegalVersion.instrument_id == instrument.id)
            )
        )
        assert [version.version_label for version in versions] == ["2026年"]
        assert versions[0].artifact_id == document_a
        provisions = list(
            await session.scalars(
                select(ProvisionVersion).where(ProvisionVersion.legal_version_id == versions[0].id)
            )
        )
        assert len(provisions) == 1


async def test_withdraw_cancels_pending_parse_jobs(
    make_client, make_user, storage, session_factory
):
    """撤下原件要顺带取消它尚未开始的解析任务。

    否则任务指向的原件已不存在，只会以 document_missing 永久失败进人工队列（实跑时 6 份原件
    留下 6 条噪音）。设计 §12.2 规定取消只作用于未开始的任务。
    """
    name = law_name()
    editor, document_id = await import_document_for_parsing(
        make_client, make_user, law_text(name), f"{name}.txt"
    )

    async with session_factory() as session:
        job_id = await session.scalar(
            select(Job.id).where(Job.payload["document_id"].astext == str(document_id))
        )
        assert job_id is not None
        assert (await session.get(Job, job_id)).status == "pending"

    async with session_factory() as session:
        await withdraw_documents(session, _principal(editor), storage, [document_id], reason=REASON)

    async with session_factory() as session:
        assert (await session.get(Job, job_id)).status == "cancelled"


async def test_withdraw_removes_identities_orphaned_by_removed_versions(
    make_client, make_user, storage, session_factory
):
    """版本被删后，只为该版本存在的条款身份要一并清理（设计 §5.3 不允许孤立引用）。

    本体因还有其他版本而存活时，早先的实现会把这些身份留在库里当孤立行。
    """
    name = law_name()
    editor, _document_a = await _parse(
        make_client,
        make_user,
        storage,
        session_factory,
        law_text(name, phone="13800138000"),
        f"{name}.txt",
    )
    # 另一个版本，多出「第三条」——该条款身份只属于这个版本
    _editor_c, document_c = await _parse(
        make_client,
        make_user,
        storage,
        session_factory,
        law_text(name, passed="2025年1月1日", extra_article=True),
        f"{name}.txt",
    )

    async with session_factory() as session:
        instrument = await session.scalar(
            select(LegalInstrument).where(LegalInstrument.title == name)
        )
        assert sorted(
            identity.provision_number
            for identity in await session.scalars(
                select(ProvisionIdentity).where(ProvisionIdentity.instrument_id == instrument.id)
            )
        ) == ["1", "2", "3"]

    async with session_factory() as session:
        report = await withdraw_documents(
            session, _principal(editor), storage, [document_c], reason=REASON
        )
    assert report.remove_versions == ("2025年",)
    assert report.remove_instruments == ()

    async with session_factory() as session:
        instrument = await session.scalar(
            select(LegalInstrument).where(LegalInstrument.title == name)
        )
        numbers = sorted(
            identity.provision_number
            for identity in await session.scalars(
                select(ProvisionIdentity).where(ProvisionIdentity.instrument_id == instrument.id)
            )
        )
        # 「3」随被删版本一起清掉；「1」「2」仍被存活的版本使用
        assert numbers == ["1", "2"]
        withdrawn = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "document.withdrawn",
                AuditEvent.resource_id == document_c,
            )
        )
        assert withdrawn.payload["removed_provision_identities"] == 1


async def test_withdraw_dry_run_changes_nothing(make_client, make_user, storage, session_factory):
    name = law_name()
    editor, document_id = await _parse(
        make_client, make_user, storage, session_factory, law_text(name), f"{name}.txt"
    )

    async with session_factory() as session:
        report = await withdraw_documents(
            session, _principal(editor), storage, [document_id], reason=REASON, dry_run=True
        )

    assert report.documents[0].parse_revisions == 1
    assert report.remove_instruments == (name,)
    async with session_factory() as session:
        assert await session.get(SourceArtifact, document_id) is not None
        assert (
            await session.scalar(
                select(func.count(ParseRevision.id)).where(ParseRevision.artifact_id == document_id)
            )
            == 1
        )
        assert (
            await session.scalar(select(LegalInstrument).where(LegalInstrument.title == name))
            is not None
        )
        assert (
            await session.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.action == "document.withdrawn",
                    AuditEvent.resource_id == document_id,
                )
            )
            == 0
        )


async def test_withdraw_requires_a_reason(make_client, make_user, storage, session_factory):
    name = law_name()
    editor, document_id = await _parse(
        make_client, make_user, storage, session_factory, law_text(name), f"{name}.txt"
    )

    async with session_factory() as session:
        with pytest.raises(HTTPException) as raised:
            await withdraw_documents(
                session, _principal(editor), storage, [document_id], reason="   "
            )
    assert raised.value.status_code == 422

    async with session_factory() as session:
        assert await session.get(SourceArtifact, document_id) is not None
