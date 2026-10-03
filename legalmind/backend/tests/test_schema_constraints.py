"""数据约束集成测试：业务表外键（设计 5.3「用明确的数据约束防止孤立引用」）。

需要真实 PostgreSQL（TEST_DATABASE_URL），未设置时随集成测试一并跳过。
"""

from uuid import uuid4

import pytest
import sqlalchemy as sa

from app.core.security import hash_password
from app.models import (
    Chunk,
    ChunkSpan,
    Organization,
    ParseRevision,
    Source,
    SourceArtifact,
    User,
    WikiPage,
)

pytestmark = pytest.mark.anyio

# (表, 列, 目标表)，与迁移 0006 / 0007 一致
EXPECTED_FOREIGN_KEYS = (
    ("wiki_pages", "organization_id", "organizations"),
    ("wiki_revisions", "author_id", "users"),
    ("audit_events", "organization_id", "organizations"),
    ("audit_events", "actor_id", "users"),
    ("outbox_events", "organization_id", "organizations"),
    ("access_grants", "organization_id", "organizations"),
    ("access_grants", "granted_by", "users"),
    ("sources", "organization_id", "organizations"),
    ("sources", "created_by", "users"),
    ("source_artifacts", "organization_id", "organizations"),
    ("source_artifacts", "created_by", "users"),
    ("jobs", "organization_id", "organizations"),
    ("parse_revisions", "organization_id", "organizations"),
    ("parse_revisions", "artifact_id", "source_artifacts"),
    ("parse_revisions", "created_by", "users"),
    ("chunks", "organization_id", "organizations"),
    ("chunks", "parse_revision_id", "parse_revisions"),
    ("chunk_spans", "chunk_id", "chunks"),
)


def _sha256() -> str:
    return uuid4().hex + uuid4().hex


async def _seed_parse_chain(session) -> Chunk:
    """建一条 org → user → source → artifact → parse_revision → chunk 的合法链路。"""
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

    source = Source(
        organization_id=organization.id,
        name=f"src-{uuid4().hex[:8]}",
        source_type="official",
        trust_level="high",
        license_note="测试来源",
        created_by=user.id,
    )
    session.add(source)
    await session.flush()

    artifact = SourceArtifact(
        organization_id=organization.id,
        source_id=source.id,
        object_key=uuid4().hex + uuid4().hex,
        sha256=_sha256(),
        size_bytes=1024,
        media_type="application/pdf",
        original_filename="example.pdf",
        sensitivity="internal",
        access_scope="organization",
        created_by=user.id,
    )
    session.add(artifact)
    await session.flush()

    parse_revision = ParseRevision(
        organization_id=organization.id,
        artifact_id=artifact.id,
        parser="test-parser",
        parser_version="1",
        config_version="v1",
        text_sha256=_sha256(),
        quality_status="ok",
        created_by=user.id,
    )
    session.add(parse_revision)
    await session.flush()

    chunk = Chunk(
        organization_id=organization.id,
        parse_revision_id=parse_revision.id,
        ordinal=0,
        text="第一条 测试条文",
        text_sha256=_sha256(),
    )
    session.add(chunk)
    await session.flush()
    return chunk


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
        chunk = await _seed_parse_chain(session)
        session.add(
            ChunkSpan(
                chunk_id=chunk.id,
                ordinal=0,
                char_start=10,
                char_end=10,
                text_sha256=_sha256(),
            )
        )
        with pytest.raises(sa.exc.IntegrityError):
            await session.flush()
        await session.rollback()


async def test_chunk_span_rejects_bbox_without_coordinate_system(session_factory):
    async with session_factory() as session:
        chunk = await _seed_parse_chain(session)
        session.add(
            ChunkSpan(
                chunk_id=chunk.id,
                ordinal=0,
                char_start=0,
                char_end=5,
                bbox=[0.1, 0.2, 0.3, 0.4],
                text_sha256=_sha256(),
            )
        )
        with pytest.raises(sa.exc.IntegrityError):
            await session.flush()
        await session.rollback()
