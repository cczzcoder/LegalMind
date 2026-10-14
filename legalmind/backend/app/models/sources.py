"""来源与原件（设计 §7、§11.2、§20.3）。"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
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

SOURCE_TYPES = ("official", "republished", "internal")


TRUST_LEVELS = ("high", "medium", "low")


# 暂定分级，正式分级待数据分级确认（需求 2.2）后调整
SENSITIVITY_LEVELS = ("public", "internal", "confidential")


class Source(Base):
    """资料来源登记（FR-01）。"""

    __tablename__ = "sources"
    __table_args__ = (
        # 公共数据全局共享（设计 21.2）：来源名全库唯一
        UniqueConstraint("name", name="uq_source_name"),
        CheckConstraint(_in("source_type", SOURCE_TYPES), name="ck_source_type"),
        CheckConstraint(_in("trust_level", TRUST_LEVELS), name="ck_source_trust_level"),
    )

    id: Mapped[UUID] = mapped_column(
        primary_key=True,
        default=uuid4,
    )
    name: Mapped[str] = mapped_column(String(200))
    source_type: Mapped[str] = mapped_column(String(20))
    trust_level: Mapped[str] = mapped_column(String(20))
    url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    publisher: Mapped[str | None] = mapped_column(String(200), nullable=True)
    license_note: Mapped[str] = mapped_column(Text)
    # 来源最后核查时间；为空表示未知，不用入库时间代替（FR-01）
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )


class SourceArtifact(Base):
    """原始文件记录（FR-02、设计 7.3）。只有完整写入存储后才插入，记录存在即可用。"""

    __tablename__ = "source_artifacts"
    __table_args__ = (
        # 公共数据全局共享（设计 21.2）：同一原件全库只登记一次
        UniqueConstraint("sha256", name="uq_source_artifact_sha256"),
        CheckConstraint(_in("sensitivity", SENSITIVITY_LEVELS), name="ck_artifact_sensitivity"),
        CheckConstraint(
            "access_scope IN ('organization', 'restricted')",
            name="ck_artifact_access_scope",
        ),
        # 机密资料不得对整个组织开放
        CheckConstraint(
            "sensitivity <> 'confidential' OR access_scope = 'restricted'",
            name="ck_artifact_confidential_restricted",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        primary_key=True,
        default=uuid4,
    )
    source_id: Mapped[UUID] = mapped_column(
        ForeignKey("sources.id", ondelete="RESTRICT"),
        index=True,
    )
    # 系统生成的逻辑对象键，不含上传文件名，也不是宿主机绝对路径
    object_key: Mapped[str] = mapped_column(String(200), unique=True)
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    media_type: Mapped[str] = mapped_column(String(100))
    # 仅作记录展示，不参与存储路径
    original_filename: Mapped[str] = mapped_column(String(255))
    sensitivity: Mapped[str] = mapped_column(String(20))
    access_scope: Mapped[str] = mapped_column(String(20))
    # 从来源获取文件的时间；为空表示未知
    acquired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
