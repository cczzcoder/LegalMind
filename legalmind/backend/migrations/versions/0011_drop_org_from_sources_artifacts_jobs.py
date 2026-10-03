"""Drop organisation scoping from sources, source_artifacts and jobs (design 21.2).

公共法律数据全局共享一份，来源登记、原件与解析任务都不再按组织分区，
唯一键相应收窄为全库唯一：来源名、原件 sha256、任务幂等键。

注意：本迁移的 downgrade 是有损的——被删除的组织归属无法恢复，
故回滚时该列只能以可空形式加回。执行前须确认库内不存在同名来源、
同哈希原件或同幂等键任务，否则唯一约束创建会失败。
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

TABLES = ("sources", "source_artifacts", "jobs")

# (表, 唯一约束名, 收窄后的列, 原列)
UNIQUE_KEYS = (
    ("sources", "uq_source_name", ["name"], ["organization_id", "name"]),
    ("source_artifacts", "uq_source_artifact_sha256", ["sha256"], ["organization_id", "sha256"]),
    ("jobs", "uq_job_idempotency", ["idempotency_key"], ["organization_id", "idempotency_key"]),
)


def _fk_name(table: str) -> str:
    return f"{table}_organization_id_fkey"


def _index_name(table: str) -> str:
    return f"ix_{table}_organization_id"


def upgrade():
    # 先收窄唯一键，再删列（原唯一键包含待删列）
    for table, name, columns, _ in UNIQUE_KEYS:
        op.drop_constraint(name, table, type_="unique")
        op.create_unique_constraint(name, table, columns)

    for table in TABLES:
        op.drop_index(_index_name(table), table_name=table)
        op.drop_constraint(_fk_name(table), table, type_="foreignkey")
        op.drop_column(table, "organization_id")


def downgrade():
    for table in TABLES:
        op.add_column(table, sa.Column("organization_id", sa.Uuid(), nullable=True))
        op.create_foreign_key(
            _fk_name(table),
            table,
            "organizations",
            ["organization_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        op.create_index(_index_name(table), table, ["organization_id"])

    for table, name, _, original in UNIQUE_KEYS:
        op.drop_constraint(name, table, type_="unique")
        op.create_unique_constraint(name, table, original)
