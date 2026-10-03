"""Legal version metadata: legal status and instrument title identity (design 5.1, 5.2, 8.3).

法律版本落库所需的两处结构改动：

- ``legal_versions.legal_status``：版本效力状态（``effective`` 当前有效 /
  ``not_yet_effective`` 已公布未生效 / ``repealed`` 已废止或被取代 / ``unknown`` 无法判定）。
  由公布信息中的施行日期推导，再与同一法律的其他版本比较；检索默认屏蔽
  ``not_yet_effective``（设计 8.3）。
- ``legal_instruments`` 增加 ``(jurisdiction, title)`` 唯一键。原 ``stable_id`` 依赖外部权威
  库、本地无来源，多为 NULL，而 PostgreSQL 的唯一约束允许多个 NULL，无法承担去重身份。

downgrade 只回退结构与约束；``title`` 若被规范化改写则无法还原（有损）。
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

# 与 models.LEGAL_STATUSES 一致
LEGAL_STATUSES = ("effective", "not_yet_effective", "repealed", "unknown")


def upgrade():
    op.add_column(
        "legal_versions",
        sa.Column("legal_status", sa.String(30), nullable=False, server_default="unknown"),
    )
    op.create_check_constraint(
        "ck_legal_version_legal_status",
        "legal_versions",
        "legal_status IN ('" + "', '".join(LEGAL_STATUSES) + "')",
    )
    op.create_unique_constraint(
        "uq_legal_instrument_title",
        "legal_instruments",
        ["jurisdiction", "title"],
    )


def downgrade():
    op.drop_constraint("uq_legal_instrument_title", "legal_instruments", type_="unique")
    op.drop_constraint("ck_legal_version_legal_status", "legal_versions", type_="check")
    op.drop_column("legal_versions", "legal_status")
