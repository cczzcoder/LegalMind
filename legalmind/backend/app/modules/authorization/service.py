"""操作授权与对象级授权（FR-10、设计 11.2）。

权限只由服务端角色表和 AccessGrant 决定，不接受客户端或模型提交的权限字段。
角色决定能做哪类操作；对象范围决定能对哪些对象做：同组织，且受限页面需要授权记录。
"""

from typing import Annotated

from fastapi import Depends, HTTPException
from sqlalchemy import ColumnElement, exists, or_, select

from app.core.security import Principal, get_principal
from app.models import AccessGrant, SourceArtifact, WikiPage

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

_READER = {WIKI_READ, DOCUMENT_READ, DOCUMENT_DOWNLOAD}

# 系统管理员不自动拥有业务内容权限（需求第 3 节）
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "reader": frozenset(_READER),
    "editor": frozenset(_READER | {WIKI_WRITE, DOCUMENT_WRITE}),
    "legal_reviewer": frozenset(_READER),
    "knowledge_admin": frozenset(
        _READER | {WIKI_WRITE, WIKI_GRANT, DOCUMENT_WRITE, DOCUMENT_GRANT, SOURCE_MANAGE}
    ),
    "system_admin": frozenset({USER_MANAGE}),
    "auditor": frozenset(),
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
        return (WikiPage.organization_id == principal.organization_id) & or_(
            WikiPage.access_scope == "organization",
            granted,
        )

    @staticmethod
    def document_scope(principal: Principal) -> ColumnElement[bool]:
        """当前用户可访问的原始资料范围，规则与 wiki_page_scope 相同。"""
        granted = exists(
            select(AccessGrant.id).where(
                AccessGrant.resource_type == "document",
                AccessGrant.resource_id == SourceArtifact.id,
                AccessGrant.user_id == principal.user_id,
            )
        )
        return (SourceArtifact.organization_id == principal.organization_id) & or_(
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
