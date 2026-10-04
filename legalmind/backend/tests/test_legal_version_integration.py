"""法律版本落库集成测试（设计 §5.1、§5.2、§7、§8.3）：需要 TEST_DATABASE_URL。

覆盖：解析后自动挂版本树（本体/版本/条款身份/条款版本）、条款文本为脱敏文本且绑定该条首个分块、
重复解析幂等、非法律文本不落库、同一版本多个原件的冲突与效力状态合并。
"""

from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.adapters.storage import LocalFileStorage, get_storage
from app.core.security import Principal
from app.models import (
    AuditEvent,
    LegalInstrument,
    LegalVersion,
    ProvisionIdentity,
    ProvisionVersion,
    SourceArtifact,
)
from app.modules.parsing.service import parse_artifact
from tests.helpers import import_document_for_parsing, import_document_into_source, setup_source

pytestmark = pytest.mark.anyio


@pytest.fixture
def storage(tmp_path, make_client):
    from app.main import app

    storage = LocalFileStorage(tmp_path)
    app.dependency_overrides[get_storage] = lambda: storage
    return storage


def _principal(user) -> Principal:
    return Principal(
        organization_id=user.organization_id,
        user_id=user.id,
        roles=frozenset(),
    )


def law_name() -> str:
    """每次用唯一名称：本体身份是（法域, 名称），同名会在测试间互相干扰。"""
    return f"示例测试法（{uuid4().hex[:8]}）"


def law_text(
    name: str,
    *,
    effective: str = "",
    phone: str | None = None,
    passed: str = "2026年5月1日",
) -> bytes:
    """构造一份可识别结构的最小法律文本；``effective`` 追加「自…起施行」子句。"""
    clause = f"　{effective}" if effective else ""
    body = "第一条　为了测试，制定本法。"
    if phone:
        body += f"\n第二条　联系方式为{phone}，请依法处理。"
    return (
        f"{name}\n（{passed}第十四届全国人民代表大会常务委员会第一次会议通过{clause}）\n{body}\n"
    ).encode()


async def test_parse_links_the_legal_version_tree(make_client, make_user, storage, session_factory):
    name = law_name()
    editor, document_id = await import_document_for_parsing(
        make_client, make_user, law_text(name, phone="13800138000"), f"{name}.txt"
    )

    async with session_factory() as session:
        await parse_artifact(session, storage, _principal(editor), document_id)

    async with session_factory() as session:
        instrument = await session.scalar(
            select(LegalInstrument).where(LegalInstrument.title == name)
        )
        assert instrument is not None
        assert instrument.instrument_type == "law"
        assert instrument.issuing_body == "全国人民代表大会常务委员会"
        assert instrument.jurisdiction == "中国"

        version = await session.scalar(
            select(LegalVersion).where(LegalVersion.instrument_id == instrument.id)
        )
        assert version.version_label == "2026年"
        assert version.promulgated_on == date(2026, 5, 1)
        # 没有「自…起施行」子句：生效日期不虚构
        assert version.effective_from is None
        assert version.legal_status == "unknown"
        # 正文提取完整、无回退、无冲突 → 自动确认（质量门禁）
        assert version.review_status == "approved"
        assert version.artifact_id == document_id

        identities = (
            await session.scalars(
                select(ProvisionIdentity).where(ProvisionIdentity.instrument_id == instrument.id)
            )
        ).all()
        assert sorted(identity.provision_number for identity in identities) == ["1", "2"]
        assert {identity.provision_type for identity in identities} == {"article"}

        provisions = (
            await session.scalars(
                select(ProvisionVersion)
                .where(ProvisionVersion.legal_version_id == version.id)
                .order_by(ProvisionVersion.text)
            )
        ).all()
        assert len(provisions) == 2
        first = next(p for p in provisions if p.text.startswith("第一条"))
        assert first.structure_path["article_number"] == "1"
        assert first.text_sha256 == _sha256(first.text)

        # 条款文本入库的是**脱敏文本**（设计 §21.3），明文不进库
        second = next(p for p in provisions if p.text.startswith("第二条"))
        assert "[电话_1]" in second.text
        assert "13800138000" not in second.text

        # 条款版本绑定该条**首个**分块，供沿 chunk_spans 解析原文定位
        from app.models import Chunk

        chunk = await session.get(Chunk, first.chunk_id)
        assert chunk.structure_path["article_number"] == "1"
        assert chunk.text == first.text

        parsed = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "document.parsed", AuditEvent.resource_id == document_id
            )
        )
        assert parsed.payload["legal_version_id"] == str(version.id)
        assert parsed.payload["provision_count"] == 2
        linked = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "document.legal_version_linked",
                AuditEvent.resource_id == document_id,
            )
        )
        assert linked.payload["review_status"] == "approved"


async def test_reparse_does_not_duplicate_the_version_tree(
    make_client, make_user, storage, session_factory
):
    name = law_name()
    editor, document_id = await import_document_for_parsing(
        make_client, make_user, law_text(name), f"{name}.txt"
    )
    async with session_factory() as session:
        await parse_artifact(session, storage, _principal(editor), document_id)
    async with session_factory() as session:
        await parse_artifact(session, storage, _principal(editor), document_id)

    async with session_factory() as session:
        instrument = await session.scalar(
            select(LegalInstrument).where(LegalInstrument.title == name)
        )
        versions = await session.scalar(
            select(func.count(LegalVersion.id)).where(LegalVersion.instrument_id == instrument.id)
        )
        provisions = await session.scalar(
            select(func.count(ProvisionVersion.id)).where(
                ProvisionVersion.legal_version_id.in_(
                    select(LegalVersion.id).where(LegalVersion.instrument_id == instrument.id)
                )
            )
        )
    assert versions == 1
    assert provisions == 1


async def test_non_legal_document_is_not_linked(make_client, make_user, storage, session_factory):
    name = law_name()
    # 原件按 sha256 全库唯一（设计 §21.2）：内容必须每次不同，否则撞上一次运行的残留
    content = f"一份普通说明文档。\n没有条文结构。{uuid4().hex}\n".encode()
    editor, document_id = await import_document_for_parsing(
        make_client, make_user, content, f"{name}.txt"
    )
    async with session_factory() as session:
        await parse_artifact(session, storage, _principal(editor), document_id)

    async with session_factory() as session:
        instrument = await session.scalar(
            select(LegalInstrument).where(LegalInstrument.title == name)
        )
    assert instrument is None


async def test_second_artifact_for_the_same_version_is_a_conflict(
    make_client, make_user, storage, session_factory
):
    """同一 (法, 版本标识) 的第二份原件：按效力状态择优、合并证据、降级为待审核。"""
    name = law_name()
    # 第一份：没有施行日期 → 状态 unknown
    editor_a, document_a = await import_document_for_parsing(
        make_client, make_user, law_text(name), f"{name}.txt"
    )
    async with session_factory() as session:
        await parse_artifact(session, storage, _principal(editor_a), document_a)

    # 第二份：同版本，但给出了「自 2027 起施行」→ 状态 not_yet_effective，排序更优
    editor_b, document_b = await import_document_for_parsing(
        make_client, make_user, law_text(name, effective="自2027年1月1日起施行"), f"{name}.txt"
    )
    async with session_factory() as session:
        await parse_artifact(session, storage, _principal(editor_b), document_b)

    async with session_factory() as session:
        instrument = await session.scalar(
            select(LegalInstrument).where(LegalInstrument.title == name)
        )
        version = await session.scalar(
            select(LegalVersion).where(LegalVersion.instrument_id == instrument.id)
        )
        # 确定的效力状态覆盖了 unknown —— 「已公布未生效」不会被误判为有效（设计 §8.3）
        assert version.legal_status == "not_yet_effective"
        assert version.effective_from == date(2027, 1, 1)
        assert version.review_status == "pending"
        # 排序更优的原件成为该版本的原件
        assert version.artifact_id == document_b
        assert await session.get(SourceArtifact, document_b) is not None

        conflict = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "legal_version.artifact_conflict",
                AuditEvent.resource_id == document_b,
            )
        )
        assert conflict.payload["adopted_arriving"] is True
        assert conflict.payload["existing_artifact_id"] == str(document_a)


async def test_same_source_formats_are_not_a_conflict(
    make_client, make_user, storage, session_factory
):
    """同一来源提供两种格式（官方库同时给 DOCX 与 PDF）不算冲突：同一来源对同一版本背书。

    只有来源不同才是需要人工确认的冲突（见上面的用例）。实测新语料里 6 部法律同时有官方
    DOCX 与官方 PDF，若不区分就会凭空多出 6 个「待审核」。
    """
    name = law_name()
    _, editor, source_id = await setup_source(make_client, make_user)
    # 同一来源下两份同版本原件：甲带施行日期（排序更优），乙没有
    document_a = await import_document_into_source(
        make_client,
        editor,
        source_id,
        law_text(name, effective="自2027年1月1日起施行"),
        f"{name}.txt",
    )
    async with session_factory() as session:
        await parse_artifact(session, storage, _principal(editor), document_a)
    document_b = await import_document_into_source(
        make_client, editor, source_id, law_text(name), f"{name}.txt"
    )
    async with session_factory() as session:
        await parse_artifact(session, storage, _principal(editor), document_b)

    async with session_factory() as session:
        instrument = await session.scalar(
            select(LegalInstrument).where(LegalInstrument.title == name)
        )
        version = await session.scalar(
            select(LegalVersion).where(LegalVersion.instrument_id == instrument.id)
        )
        assert version.artifact_id == document_a
        assert version.legal_status == "not_yet_effective"
        # 同一来源，不算冲突 → 保持自动确认
        assert version.review_status == "approved"
        conflicts = await session.scalar(
            select(func.count(AuditEvent.id)).where(
                AuditEvent.action == "legal_version.artifact_conflict",
                AuditEvent.resource_id.in_([document_a, document_b]),
            )
        )
        assert conflicts == 0


async def test_provision_text_starts_at_the_article_not_the_heading(
    make_client, make_user, storage, session_factory
):
    """分块段首刻意含编/章/节标题（设计 §6），但条款文本必须是「条」本身。

    实测监狱法：条款文本曾变成「第一章 总 则\\n第一条 …」——分块自带章节上下文是对的，
    但把分块文本直接拼成条款文本就把标题也算进去了。
    """
    name = law_name()
    content = (
        f"{name}\n（2026年5月1日第十四届全国人民代表大会常务委员会第一次会议通过）\n"
        "第一章　总　　则\n第一条　为了测试，制定本法。\n"
        "第二章　附　　则\n第二条　本法自公布之日起施行。\n"
    ).encode()
    editor, document_id = await import_document_for_parsing(
        make_client, make_user, content, f"{name}.txt"
    )
    async with session_factory() as session:
        await parse_artifact(session, storage, _principal(editor), document_id)

    async with session_factory() as session:
        instrument = await session.scalar(
            select(LegalInstrument).where(LegalInstrument.title == name)
        )
        version = await session.scalar(
            select(LegalVersion).where(LegalVersion.instrument_id == instrument.id)
        )
        provisions = {
            provision.structure_path["article_number"]: provision
            for provision in await session.scalars(
                select(ProvisionVersion).where(ProvisionVersion.legal_version_id == version.id)
            )
        }

    first, second = provisions["1"], provisions["2"]
    assert first.text.startswith("第一条")
    assert "第一章" not in first.text
    assert second.text.startswith("第二条")
    assert "第二章" not in second.text
    # 章节标题仍在分块里（分块自带上下文），只是不再混进条款文本
    from app.models import Chunk

    async with session_factory() as session:
        chunk = await session.get(Chunk, first.chunk_id)
    assert "第一章" in chunk.text
    assert first.chunk_id is not None


async def test_new_effective_version_repeals_the_older_one(
    make_client, make_user, storage, session_factory
):
    """同一法律的新版本已生效时，公布更早且同样有效的旧版本标记为被取代（设计 §5.3）。"""
    name = law_name()
    editor_a, document_a = await import_document_for_parsing(
        make_client,
        make_user,
        law_text(name, effective="自2025年6月1日起施行", passed="2025年1月1日"),
        f"{name}.txt",
    )
    async with session_factory() as session:
        await parse_artifact(session, storage, _principal(editor_a), document_a)

    editor_b, document_b = await import_document_for_parsing(
        make_client,
        make_user,
        law_text(name, effective="自2026年6月1日起施行", passed="2026年5月1日"),
        f"{name}.txt",
    )
    async with session_factory() as session:
        await parse_artifact(session, storage, _principal(editor_b), document_b)

    async with session_factory() as session:
        instrument = await session.scalar(
            select(LegalInstrument).where(LegalInstrument.title == name)
        )
        versions = {
            version.version_label: version
            for version in (
                await session.scalars(
                    select(LegalVersion).where(LegalVersion.instrument_id == instrument.id)
                )
            ).all()
        }
        assert versions["2025年"].legal_status == "repealed"
        assert versions["2026年"].legal_status == "effective"
        repealed = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "legal_version.repealed",
                AuditEvent.resource_id == versions["2025年"].id,
            )
        )
        assert repealed.payload["repealed_by_version_id"] == str(versions["2026年"].id)


def _sha256(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()
