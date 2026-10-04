"""按当前择优规则重挂版本树（设计 §7、§8.3）：需要 TEST_DATABASE_URL。

重挂解决的是「规则变了，但已经落库的主原件不会自动重选」——V1.11 把「格式」提到「导入时间」
之前，存量数据里后导入的 pdf 仍占着主原件。这里覆盖：重挂换成更优的那一份、预演不改数据、
取不到解析产物的原件跳过并如实报告。
"""

from uuid import uuid4

import pytest
from sqlalchemy import select

from app.adapters.storage import LocalFileStorage, get_storage
from app.core.security import Principal
from app.models import AuditEvent, LegalInstrument, LegalVersion, SourceArtifact
from app.modules.legal_corpus import relink
from app.modules.parsing.service import parse_artifact
from tests.helpers import (
    build_minimal_docx,
    import_document_into_source,
    setup_source,
    sha256_hex,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def storage(tmp_path, make_client):
    from app.main import app

    storage = LocalFileStorage(tmp_path)
    app.dependency_overrides[get_storage] = lambda: storage
    return storage


def _principal(user) -> Principal:
    return Principal(organization_id=user.organization_id, user_id=user.id, roles=frozenset())


def _law_text(name: str) -> str:
    return (
        f"{name}\n（2026年5月1日第十四届全国人民代表大会常务委员会第一次会议通过"
        "　自2027年1月1日起施行）\n第一条　为了测试，制定本法。\n"
    )


async def _two_formats(make_client, make_user, storage, session_factory):
    """同一来源、同一版本各一份 docx 与 txt，再把主原件**退回**给后导入的 txt。

    退回是模拟旧规则选出的结果（旧规则里导入时间排在格式之前，后导入的 txt 会当上主原件）；
    两份原件都是确定效力状态，因此状态与公布日期两条轴都打平，只剩格式决定胜负。
    """
    _, editor, source_id = await setup_source(make_client, make_user)
    name = f"示例重挂法（{uuid4().hex[:8]}）"
    text = _law_text(name)
    docx_id = await import_document_into_source(
        make_client, editor, source_id, build_minimal_docx(text.splitlines()), f"{name}.docx"
    )
    async with session_factory() as session:
        await parse_artifact(session, storage, _principal(editor), docx_id)
    txt_id = await import_document_into_source(
        make_client, editor, source_id, text.encode(), f"{name}.txt"
    )
    async with session_factory() as session:
        await parse_artifact(session, storage, _principal(editor), txt_id)

    async with session_factory() as session:
        version = await session.scalar(
            select(LegalVersion)
            .join(LegalInstrument, LegalInstrument.id == LegalVersion.instrument_id)
            .where(LegalInstrument.title == name)
        )
        version.artifact_id = txt_id
        instrument_id = version.instrument_id
        await session.commit()
    return editor, name, docx_id, txt_id, instrument_id


async def test_relink_switches_the_primary_to_the_better_format(
    make_client, make_user, storage, session_factory
):
    editor, name, docx_id, _txt_id, instrument_id = await _two_formats(
        make_client, make_user, storage, session_factory
    )
    async with session_factory() as session:
        report = await relink.relink_versions(
            session, _principal(editor), instrument_ids={instrument_id}
        )
    async with session_factory() as session:
        version = await session.scalar(
            select(LegalVersion).where(LegalVersion.instrument_id == instrument_id)
        )

    assert report.instruments == 1
    assert report.relinked == 2
    assert version.artifact_id == docx_id
    change = next(item for item in report.changed if item.instrument_title == name)
    assert change.previous_artifact == f"{name}.txt"
    assert change.adopted_artifact == f"{name}.docx"
    assert report.created_versions == ()


async def test_dry_run_reports_the_change_without_making_it(
    make_client, make_user, storage, session_factory
):
    editor, name, _docx_id, txt_id, instrument_id = await _two_formats(
        make_client, make_user, storage, session_factory
    )
    async with session_factory() as session:
        report = await relink.relink_versions(
            session, _principal(editor), instrument_ids={instrument_id}, dry_run=True
        )
    async with session_factory() as session:
        version = await session.scalar(
            select(LegalVersion).where(LegalVersion.instrument_id == instrument_id)
        )

    assert [item.instrument_title for item in report.changed] == [name]
    # 预演算出了报告，但库里没动
    assert version.artifact_id == txt_id


async def test_artifact_without_a_parse_product_is_skipped(
    make_client, make_user, storage, session_factory
):
    """挂过版本但没有解析产物的原件（如被清理过）跳过并如实报告，不猜。"""
    editor, _name, docx_id, _txt_id, instrument_id = await _two_formats(
        make_client, make_user, storage, session_factory
    )
    async with session_factory() as session:
        source_id = await session.scalar(
            select(SourceArtifact.source_id).where(SourceArtifact.id == docx_id)
        )
        ghost = SourceArtifact(
            source_id=source_id,
            object_key=uuid4().hex + uuid4().hex,
            sha256=sha256_hex(),
            size_bytes=16,
            media_type="application/pdf",
            original_filename="没有解析产物的原件.pdf",
            sensitivity="public",
            access_scope="organization",
            created_by=editor.id,
        )
        session.add(ghost)
        await session.flush()
        session.add(
            AuditEvent(
                organization_id=editor.organization_id,
                actor_id=editor.id,
                action="document.legal_version_linked",
                resource_id=ghost.id,
                payload={
                    "document_id": str(ghost.id),
                    "instrument_id": str(instrument_id),
                },
            )
        )
        await session.commit()

    async with session_factory() as session:
        report = await relink.relink_versions(
            session, _principal(editor), instrument_ids={instrument_id}
        )
    assert report.skipped == ("没有解析产物的原件.pdf",)
