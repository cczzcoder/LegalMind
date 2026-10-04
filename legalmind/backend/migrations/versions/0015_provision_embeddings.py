"""Provision embeddings for the vector path (design 8.2, 8.3, 14.1).

向量通路的数据底座：

- 启用 ``vector`` 扩展。它随 PostgreSQL 提供（compose 换成带 pgvector 的官方镜像），
  **不是新增常驻组件**——设计 §14.1 的常驻服务清单写的就是「PostgreSQL（含后续 pgvector）」。
- ``provision_embeddings``：条款版本的向量，按 ``(provision_version_id, model)`` 唯一，
  于是**同一个条款可以并存多个模型的向量**，评测时直接横向比，不必反复重算。

**维度故意不写死**：不同模型维度不同（BGE-M3 是 1024、text2vec-base 是 768），用不定长
``vector`` 列承载、配 ``dimensions`` 列自述。代价是**建不了 ANN 索引**，只能精确最近邻——
设计 §8.3 本来就要求「默认执行精确最近邻搜索，近似索引按数据量和评测结果再启用」，
所以这不构成偏差；真要上索引时应按模型拆表，或改成维度固定的列。
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "provision_embeddings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "provision_version_id",
            sa.Uuid(),
            sa.ForeignKey("provision_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        # 模型标识（仓库名 + 版本，如 BAAI/bge-m3）：同一条款可存多个模型的向量
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("embedding", Vector(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("provision_version_id", "model", name="uq_provision_embedding_model"),
    )
    op.create_index("ix_provision_embeddings_model", "provision_embeddings", ["model"])


def downgrade():
    op.drop_index("ix_provision_embeddings_model", table_name="provision_embeddings")
    op.drop_table("provision_embeddings")
    # 不 DROP EXTENSION：同一库里可能有别的对象在用，且删除扩展可能级联删除列
