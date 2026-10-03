"""Foreign keys on business tables (design 5.3).

业务表的 organization_id 与"人"的引用列（created_by / author_id / granted_by /
actor_id）统一建外键，ondelete 一律 RESTRICT：不隐式级联删除，物理删除交由设计
15.3 的显式删除流程。多态引用（access_grants.resource_id、audit_events.resource_id）
无法建外键，不在本迁移范围。

约束名沿用 PostgreSQL 默认的 <表>_<列>_fkey，与建表时自动生成的既有外键一致。
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

# (表, 列, 目标表)
FOREIGN_KEYS = (
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


def _name(table: str, column: str) -> str:
    return f"{table}_{column}_fkey"


def upgrade():
    for table, column, target in FOREIGN_KEYS:
        op.create_foreign_key(
            _name(table, column),
            table,
            target,
            [column],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade():
    for table, column, _ in FOREIGN_KEYS:
        op.drop_constraint(_name(table, column), table, type_="foreignkey")
