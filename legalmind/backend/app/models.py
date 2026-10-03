from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class WikiPage(Base):
    __tablename__ = "wiki_pages"
    __table_args__ = (
        CheckConstraint(
            "head_revision >= 1",
            name="ck_wiki_page_head_positive",
        ),
        CheckConstraint(
            "access_scope IN ('organization', 'restricted')",
            name="ck_wiki_page_access_scope",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        primary_key=True,
        default=uuid4,
    )
    organization_id: Mapped[UUID] = mapped_column(index=True)
    title: Mapped[str] = mapped_column(String(200))
    head_revision: Mapped[int] = mapped_column(Integer)
    # organization：同组织有角色权限者可访问；restricted：还需 AccessGrant
    access_scope: Mapped[str] = mapped_column(
        String(20),
        default="organization",
        server_default="organization",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )


class WikiRevision(Base):
    __tablename__ = "wiki_revisions"
    __table_args__ = (
        UniqueConstraint(
            "page_id",
            "number",
            name="uq_wiki_revision_number",
        ),
        CheckConstraint(
            "number > 0",
            name="ck_wiki_revision_positive",
        ),
        CheckConstraint(
            "status = 'draft'",
            name="ck_wiki_draft_only",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        primary_key=True,
        default=uuid4,
    )
    page_id: Mapped[UUID] = mapped_column(
        ForeignKey("wiki_pages.id", ondelete="RESTRICT"),
        index=True,
    )
    number: Mapped[int] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(30),
        default="draft",
    )
    author_id: Mapped[UUID]

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[UUID] = mapped_column(
        primary_key=True,
        default=uuid4,
    )
    organization_id: Mapped[UUID] = mapped_column(index=True)
    # 匿名操作（如未登录者的失败登录）没有操作者
    actor_id: Mapped[UUID | None]
    action: Mapped[str] = mapped_column(String(100))
    resource_id: Mapped[UUID]
    payload: Mapped[dict] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )


class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    id: Mapped[UUID] = mapped_column(
        primary_key=True,
        default=uuid4,
    )
    organization_id: Mapped[UUID] = mapped_column(index=True)
    event_type: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


ROLE_NAMES = (
    "reader",
    "editor",
    "legal_reviewer",
    "knowledge_admin",
    "system_admin",
    "auditor",
)


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[UUID] = mapped_column(
        primary_key=True,
        default=uuid4,
    )
    name: Mapped[str] = mapped_column(String(200), unique=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(
        primary_key=True,
        default=uuid4,
    )
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        index=True,
    )
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # TOTP 密钥以 Fernet 加密保存；mfa_enabled_at 为空表示尚未完成绑定
    mfa_secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    mfa_enabled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # 最近一次成功使用的 TOTP 时间步，防止同一验证码重放
    mfa_last_timecode: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )


class UserRole(Base):
    __tablename__ = "user_roles"
    __table_args__ = (
        CheckConstraint(
            "role IN ('" + "', '".join(ROLE_NAMES) + "')",
            name="ck_user_role_known",
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role: Mapped[str] = mapped_column(String(30), primary_key=True)


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[UUID] = mapped_column(
        primary_key=True,
        default=uuid4,
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    # 只保存会话令牌的 SHA-256，数据库泄露不能直接冒用会话
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_token: Mapped[str] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # 本会话通过第二因素验证的时间；为空表示只完成了密码验证
    mfa_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class MfaRecoveryCode(Base):
    __tablename__ = "mfa_recovery_codes"

    id: Mapped[UUID] = mapped_column(
        primary_key=True,
        default=uuid4,
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    # 恢复码为高熵随机值，只保存 SHA-256
    code_hash: Mapped[str] = mapped_column(String(64))
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class AccessGrant(Base):
    """用户对单个资源的访问授权（设计 5.1）。操作类型仍由角色权限决定。"""

    __tablename__ = "access_grants"
    __table_args__ = (
        UniqueConstraint(
            "resource_type",
            "resource_id",
            "user_id",
            name="uq_access_grant",
        ),
        CheckConstraint(
            "resource_type IN ('wiki_page', 'document')",
            name="ck_access_grant_resource_type",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        primary_key=True,
        default=uuid4,
    )
    organization_id: Mapped[UUID] = mapped_column(index=True)
    resource_type: Mapped[str] = mapped_column(String(30))
    resource_id: Mapped[UUID]
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    granted_by: Mapped[UUID]

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )


SOURCE_TYPES = ("official", "republished", "internal")
TRUST_LEVELS = ("high", "medium", "low")
# 暂定分级，正式分级待数据分级确认（需求 2.2）后调整
SENSITIVITY_LEVELS = ("public", "internal", "confidential")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ('" + "', '".join(values) + "')"


class Source(Base):
    """资料来源登记（FR-01）。"""

    __tablename__ = "sources"
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_source_name"),
        CheckConstraint(_in("source_type", SOURCE_TYPES), name="ck_source_type"),
        CheckConstraint(_in("trust_level", TRUST_LEVELS), name="ck_source_trust_level"),
    )

    id: Mapped[UUID] = mapped_column(
        primary_key=True,
        default=uuid4,
    )
    organization_id: Mapped[UUID] = mapped_column(index=True)
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
    created_by: Mapped[UUID]

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )


class SourceArtifact(Base):
    """原始文件记录（FR-02、设计 7.3）。只有完整写入存储后才插入，记录存在即可用。"""

    __tablename__ = "source_artifacts"
    __table_args__ = (
        UniqueConstraint("organization_id", "sha256", name="uq_source_artifact_sha256"),
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
    organization_id: Mapped[UUID] = mapped_column(index=True)
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
    created_by: Mapped[UUID]

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )


class LoginAttempt(Base):
    __tablename__ = "login_attempts"

    id: Mapped[UUID] = mapped_column(
        primary_key=True,
        default=uuid4,
    )
    username: Mapped[str] = mapped_column(String(100), index=True)
    ip_address: Mapped[str] = mapped_column(String(64), index=True)
    succeeded: Mapped[bool] = mapped_column(Boolean)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
    )


JOB_STATUSES = ("pending", "running", "retry_wait", "succeeded", "failed", "cancelled")


class Job(Base):
    """数据库任务表（设计 12.2）。当前只登记任务，worker 尚未实现。"""

    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("organization_id", "idempotency_key", name="uq_job_idempotency"),
        CheckConstraint(_in("status", JOB_STATUSES), name="ck_job_status"),
        CheckConstraint("attempt_count >= 0", name="ck_job_attempts_nonnegative"),
    )

    id: Mapped[UUID] = mapped_column(
        primary_key=True,
        default=uuid4,
    )
    organization_id: Mapped[UUID] = mapped_column(index=True)
    job_type: Mapped[str] = mapped_column(String(50))
    payload: Mapped[dict] = mapped_column(JSONB)
    # 组织 + 文件哈希 + 任务类型 + 处理配置版本（设计 7.1）
    idempotency_key: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    progress: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
