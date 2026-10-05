"""操作授权与对象级授权（FR-10、设计 11.2）。

权限只由服务端角色表和 AccessGrant 决定，不接受客户端或模型提交的权限字段。
角色决定能做哪类操作；对象范围决定能对哪些对象做：同组织，且受限页面需要授权记录。
"""

from typing import Annotated

from fastapi import Depends, HTTPException
from sqlalchemy import ColumnElement, exists, or_, select

from app.core.security import Principal, get_principal
from app.models import (
    AccessGrant,
    LegalVersion,
    ProvisionVersion,
    SourceArtifact,
    WikiPage,
    WikiRevision,
    WikiRevisionCitation,
)

WIKI_READ = "wiki.read"
WIKI_WRITE = "wiki.write"
WIKI_GRANT = "wiki.grant"
USER_MANAGE = "user.manage"
DOCUMENT_READ = "document.read"
# 下载原件与阅读元数据分开授权（FR-10）
DOCUMENT_DOWNLOAD = "document.download"
DOCUMENT_WRITE = "document.write"
DOCUMENT_GRANT = "document.grant"
SOURCE_MANAGE = "source.manage"
# 审核决定（通过 / 驳回 Wiki 修订）与查看审计记录（设计 §10.2、需求第 3 节）
REVIEW_DECIDE = "review.decide"
AUDIT_READ = "audit.read"

_READER = {WIKI_READ, DOCUMENT_READ, DOCUMENT_DOWNLOAD}

# 系统管理员不自动拥有业务内容权限（需求第 3 节）
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "reader": frozenset(_READER),
    "editor": frozenset(_READER | {WIKI_WRITE, DOCUMENT_WRITE}),
    # 法律审核员：审核 Wiki 修订与资料版本（需求第 3 节）。**不给写权限**——审核与撰写分开，
    # 否则「独立审核」无从谈起；作者也不能审自己的修订（服务层另有校验）。
    "legal_reviewer": frozenset(_READER | {REVIEW_DECIDE}),
    "knowledge_admin": frozenset(
        _READER
        | {WIKI_WRITE, WIKI_GRANT, DOCUMENT_WRITE, DOCUMENT_GRANT, SOURCE_MANAGE, REVIEW_DECIDE}
    ),
    "system_admin": frozenset({USER_MANAGE}),
    # 审计人员：查看审计与追溯记录（需求第 3 节）。只读，且**不含原件下载**——核对记录不等于
    # 取走原件。
    "auditor": frozenset({WIKI_READ, DOCUMENT_READ, AUDIT_READ}),
}


class AuthorizationService:
    @staticmethod
    def can(principal: Principal, permission: str) -> bool:
        # 未知角色不授予任何权限
        return any(
            permission in ROLE_PERMISSIONS.get(role, frozenset()) for role in principal.roles
        )

    @staticmethod
    def wiki_page_scope(principal: Principal) -> ColumnElement[bool]:
        """当前用户可访问的页面范围，作为查询条件在数据库中执行。

        每次请求实时查询授权表，撤销授权立即生效，不依赖缓存。
        """
        granted = exists(
            select(AccessGrant.id).where(
                AccessGrant.resource_type == "wiki_page",
                AccessGrant.resource_id == WikiPage.id,
                AccessGrant.user_id == principal.user_id,
            )
        )
        # §10.3 权限继承：页面引用了读者无权访问的原件时，整个页面不可见。
        # 「不能因为内容被概括或改写就自动取消原文限制」——所以判据是**引用**，不是页面的
        # access_scope。未发布的页面（published_revision 为 NULL）没有引用可查，由范围本身决定。
        leaked = exists(
            select(WikiRevisionCitation.id)
            .join(WikiRevision, WikiRevisionCitation.revision_id == WikiRevision.id)
            .join(
                ProvisionVersion, WikiRevisionCitation.provision_version_id == ProvisionVersion.id
            )
            .join(LegalVersion, ProvisionVersion.legal_version_id == LegalVersion.id)
            .join(SourceArtifact, LegalVersion.artifact_id == SourceArtifact.id)
            .where(
                WikiRevision.page_id == WikiPage.id,
                WikiRevision.number == WikiPage.published_revision,
                ~AuthorizationService.document_scope(principal),
            )
        )
        return (
            (WikiPage.organization_id == principal.organization_id)
            & or_(WikiPage.access_scope == "organization", granted)
            & ~leaked
        )

    @staticmethod
    def document_scope(principal: Principal) -> ColumnElement[bool]:
        """当前用户可访问的原始资料范围。

        原件属公共法律数据，全局共享（设计 21.2），不再按组织过滤：所有已登录用户
        均可读取组织范围的原件；restricted 原件仍需显式授权。
        """
        granted = exists(
            select(AccessGrant.id).where(
                AccessGrant.resource_type == "document",
                AccessGrant.resource_id == SourceArtifact.id,
                AccessGrant.user_id == principal.user_id,
            )
        )
        return or_(
            SourceArtifact.access_scope == "organization",
            granted,
        )


def require_permission(permission: str):
    async def dependency(
        principal: Annotated[Principal, Depends(get_principal)],
    ) -> Principal:
        if not AuthorizationService.can(principal, permission):
            raise HTTPException(status_code=403, detail="Permission denied")
        return principal

    return dependency
