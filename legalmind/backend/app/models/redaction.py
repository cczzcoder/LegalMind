"""脱敏实体（设计 §21.3）。"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, _in

# 脱敏实体类型；与 redaction/detector.py 的 ENTITY_TYPES 一致
REDACTION_ENTITY_TYPES = ("id_number", "case_number", "phone", "person")


class RedactionEntity(Base):
    """脱敏映射表（设计 21.3）。

    原文本不落库，由脱敏文本 + 本表可逆重建。本表是系统内**最敏感**的数据：
    访问权限严于业务数据，读写均写入审计，审计正文不含明文。
    """

    __tablename__ = "redaction_entities"
    __table_args__ = (
        # 同一解析版本内，同类型同原值只对应一个占位符
        UniqueConstraint(
            "parse_revision_id",
            "entity_type",
            "plaintext",
            name="uq_redaction_entity_value",
        ),
        UniqueConstraint(
            "parse_revision_id",
            "placeholder",
            name="uq_redaction_entity_placeholder",
        ),
        CheckConstraint(
            _in("entity_type", REDACTION_ENTITY_TYPES),
            name="ck_redaction_entity_type",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        primary_key=True,
        default=uuid4,
    )
    parse_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("parse_revisions.id", ondelete="RESTRICT"),
        index=True,
    )
    entity_type: Mapped[str] = mapped_column(String(30))
    # 原文中的实体值；本列是本系统内最敏感的数据
    plaintext: Mapped[str] = mapped_column(Text)
    # 入库文本中使用的占位符，如 [案号_1]
    placeholder: Mapped[str] = mapped_column(String(50))
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
