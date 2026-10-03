"""Drop organisation scoping from public legal data tables (design 21.2).

V1.5 起公共法律数据全局共享一份，公共数据表不设组织字段（设计 21.2）。
本迁移只处理 0007–0009 建立、且尚无服务层代码引用的表；
sources / source_artifacts / jobs 的同类调整会改动授权判定与测试，另行处理。

注意：本迁移的 downgrade 是有损的——被删除的组织归属无法恢复，
故回滚时该列只能以可空形式加回。
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

# 去掉组织字段的公共数据表
PUBLIC_TABLES = (
    "parse_revisions",
    "chunks",
    "legal_instruments",
    "legal_versions",
    "provision_identities",
    "provision_versions",
    "provision_relations",
    "applicability_records",
)


def _fk_name(table: str) -> str:
    return f"{table}_organization_id_fkey"


def _index_name(table: str) -> str:
    return f"ix_{table}_organization_id"


def upgrade():
    # 稳定 ID 改为全库唯一（公共数据不再按组织分区）
    op.drop_constraint("uq_legal_instrument_stable_id", "legal_instruments", type_="unique")
    op.create_unique_constraint("uq_legal_instrument_stable_id", "legal_instruments", ["stable_id"])

    for table in PUBLIC_TABLES:
        op.drop_index(_index_name(table), table_name=table)
        op.drop_constraint(_fk_name(table), table, type_="foreignkey")
        op.drop_column(table, "organization_id")


def downgrade():
    for table in PUBLIC_TABLES:
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

    op.drop_constraint("uq_legal_instrument_stable_id", "legal_instruments", type_="unique")
    op.create_unique_constraint(
        "uq_legal_instrument_stable_id",
        "legal_instruments",
        ["organization_id", "stable_id"],
    )
