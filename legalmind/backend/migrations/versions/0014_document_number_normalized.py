"""Normalised document number for exact matching (design 8.3).

设计 §8.3 要求文号「以规范化字段保存并精确匹配」，因此原样值（``document_number``，供展示）
之外再加一列 ``document_number_normalized``（供匹配，带索引）。

回填刻意复用应用里的 ``normalize_document_number``：**写入与检索必须用同一套规则**，两边各写一份
迟早分叉。代价是这条迁移的结果会随规范化规则变化——本项目接受这一点，因为规范化规则本身是设计
§8.3 的一部分，不该悄悄分叉。
"""

import sqlalchemy as sa
from alembic import op

from app.modules.legal_corpus.metadata import normalize_document_number

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

_INDEX = "ix_legal_instruments_document_number_normalized"


def upgrade():
    op.add_column(
        "legal_instruments",
        sa.Column("document_number_normalized", sa.String(200), nullable=True),
    )
    op.create_index(_INDEX, "legal_instruments", ["document_number_normalized"])

    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT id, document_number FROM legal_instruments WHERE document_number IS NOT NULL"
        )
    ).fetchall()
    for row in rows:
        connection.execute(
            sa.text(
                "UPDATE legal_instruments SET document_number_normalized = :value WHERE id = :id"
            ),
            {"value": normalize_document_number(row.document_number), "id": row.id},
        )


def downgrade():
    op.drop_index(_INDEX, table_name="legal_instruments")
    op.drop_column("legal_instruments", "document_number_normalized")
