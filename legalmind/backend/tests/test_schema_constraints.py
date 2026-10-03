"""数据约束集成测试：业务表外键（设计 5.3「用明确的数据约束防止孤立引用」）。

需要真实 PostgreSQL（TEST_DATABASE_URL），未设置时随集成测试一并跳过。
"""

from uuid import uuid4

import pytest
import sqlalchemy as sa

from app.core.security import hash_password
from app.models import (
    ChunkSpan,
    LegalInstrument,
    Organization,
    ProvisionIdentity,
    ProvisionRelation,
    User,
    WikiPage,
)
from tests.helpers import seed_parse_chain, sha256_hex

pytestmark = pytest.mark.anyio

# (表, 列, 目标表)，与迁移 0006 / 0007 / 0008 / 0010 一致
# 公共法律数据表不设组织字段（设计 21.2），故不在此列
EXPECTED_FOREIGN_KEYS = (
    ("wiki_pages", "organization_id", "organizations"),
    ("wiki_revisions", "author_id", "users"),
    ("audit_events", "organization_id", "organizations"),
    ("audit_events", "actor_id", "users"),
    ("outbox_events", "organization_id", "organizations"),
    ("access_grants", "organization_id", "organizations"),
    ("access_grants", "granted_by", "users"),
    ("sources", "created_by", "users"),
    ("source_artifacts", "created_by", "users"),
    ("parse_revisions", "artifact_id", "source_artifacts"),
    ("parse_revisions", "created_by", "users"),
    ("chunks", "parse_revision_id", "parse_revisions"),
    ("chunk_spans", "chunk_id", "chunks"),
    ("legal_instruments", "created_by", "users"),
    ("legal_versions", "instrument_id", "legal_instruments"),
    ("legal_versions", "artifact_id", "source_artifacts"),
    ("legal_versions", "created_by", "users"),
    ("provision_identities", "instrument_id", "legal_instruments"),
    ("provision_identities", "created_by", "users"),
    ("provision_versions", "legal_version_id", "legal_versions"),
    ("provision_versions", "provision_identity_id", "provision_identities"),
    ("provision_versions", "chunk_id", "chunks"),
    ("provision_versions", "created_by", "users"),
    ("provision_relations", "source_identity_id", "provision_identities"),
    ("provision_relations", "target_identity_id", "provision_identities"),
    ("provision_relations", "created_by", "users"),
    ("applicability_records", "provision_identity_id", "provision_identities"),
    ("applicability_records", "confirmed_by", "users"),
    ("applicability_records", "created_by", "users"),
    ("redaction_entities", "parse_revision_id", "parse_revisions"),
    ("redaction_entities", "created_by", "users"),
)


async def test_business_tables_enforce_foreign_keys(engine):
    def inspect_foreign_keys(sync_connection):
        inspector = sa.inspect(sync_connection)
        return {
            (table, fk["constrained_columns"][0]): fk
            for table in inspector.get_table_names()
            for fk in inspector.get_foreign_keys(table)
        }

    async with engine.connect() as connection:
        found = await connection.run_sync(inspect_foreign_keys)

    for table, column, target in EXPECTED_FOREIGN_KEYS:
        foreign_key = found.get((table, column))
        assert foreign_key is not None, f"{table}.{column} 缺少外键"
        assert foreign_key["referred_table"] == target, (
            f"{table}.{column} 指向 {foreign_key['referred_table']}，应为 {target}"
        )
        assert foreign_key["options"].get("ondelete") == "RESTRICT", (
            f"{table}.{column} 的 ondelete 应为 RESTRICT"
        )


async def test_orphan_organization_is_rejected(session_factory):
    async with session_factory() as session:
        session.add(WikiPage(organization_id=uuid4(), title="孤儿页面", head_revision=1))
        with pytest.raises(sa.exc.IntegrityError):
            await session.flush()
        await session.rollback()


async def test_chunk_span_rejects_inverted_char_range(session_factory):
    async with session_factory() as session:
        *_, chunk = await seed_parse_chain(session)
        session.add(
            ChunkSpan(
                chunk_id=chunk.id,
                ordinal=0,
                char_start=10,
                char_end=10,
                text_sha256=sha256_hex(),
            )
        )
        with pytest.raises(sa.exc.IntegrityError):
            await session.flush()
        await session.rollback()


async def test_chunk_span_rejects_bbox_without_coordinate_system(session_factory):
    async with session_factory() as session:
        *_, chunk = await seed_parse_chain(session)
        session.add(
            ChunkSpan(
                chunk_id=chunk.id,
                ordinal=0,
                char_start=0,
                char_end=5,
                bbox=[0.1, 0.2, 0.3, 0.4],
                text_sha256=sha256_hex(),
            )
        )
        with pytest.raises(sa.exc.IntegrityError):
            await session.flush()
        await session.rollback()


async def _seed_two_identities(session) -> tuple[ProvisionIdentity, ProvisionIdentity]:
    """建一条 org → user → instrument → 两个条款身份的合法链路。"""
    organization = Organization(name=f"org-{uuid4()}")
    session.add(organization)
    await session.flush()

    user = User(
        organization_id=organization.id,
        username=f"u-{uuid4().hex[:12]}",
        password_hash=hash_password("test-password"),
        is_active=True,
    )
    session.add(user)
    await session.flush()

    instrument = LegalInstrument(
        title="测试法",
        jurisdiction="CN",
        issuing_body="测试机关",
        instrument_type="law",
        created_by=user.id,
    )
    session.add(instrument)
    await session.flush()

    first = ProvisionIdentity(
        instrument_id=instrument.id,
        provision_type="article",
        provision_number="第一条",
        created_by=user.id,
    )
    second = ProvisionIdentity(
        instrument_id=instrument.id,
        provision_type="article",
        provision_number="第二条",
        created_by=user.id,
    )
    session.add_all([first, second])
    await session.flush()
    return first, second


async def test_provision_relation_rejects_self_reference(session_factory):
    """关系必须指向两个不同的条款（设计 5.1：防止孤立/无意义引用）。"""
    async with session_factory() as session:
        first, _ = await _seed_two_identities(session)
        session.add(
            ProvisionRelation(
                source_identity_id=first.id,
                target_identity_id=first.id,
                relation_type="supersede",
                created_by=first.created_by,
            )
        )
        with pytest.raises(sa.exc.IntegrityError):
            await session.flush()
        await session.rollback()
