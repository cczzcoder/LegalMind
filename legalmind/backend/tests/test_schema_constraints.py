"""数据约束集成测试：业务表外键（设计 5.3「用明确的数据约束防止孤立引用」）。

需要真实 PostgreSQL（TEST_DATABASE_URL），未设置时随集成测试一并跳过。
"""

from uuid import uuid4

import pytest
import sqlalchemy as sa

from app.models import WikiPage

pytestmark = pytest.mark.anyio

# (表, 列, 目标表)，与迁移 0006 一致
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
