"""操作授权（FR-10、设计 11.2）。

权限只由服务端角色表决定，不接受客户端或模型提交的权限字段。
对象级授权（AccessGrant）在 3b 实现；当前对象范围仅为组织隔离。
"""

from typing import Annotated

from fastapi import Depends, HTTPException

from app.core.security import Principal, get_principal

WIKI_READ = "wiki.read"
WIKI_WRITE = "wiki.write"
USER_MANAGE = "user.manage"

# 系统管理员不自动拥有业务内容权限（需求第 3 节）
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "reader": frozenset({WIKI_READ}),
    "editor": frozenset({WIKI_READ, WIKI_WRITE}),
    "legal_reviewer": frozenset({WIKI_READ}),
    "knowledge_admin": frozenset({WIKI_READ, WIKI_WRITE}),
    "system_admin": frozenset({USER_MANAGE}),
    "auditor": frozenset(),
}


class AuthorizationService:
    @staticmethod
    def can(principal: Principal, permission: str) -> bool:
        # 未知角色不授予任何权限
        return any(
            permission in ROLE_PERMISSIONS.get(role, frozenset())
            for role in principal.roles
        )


def require_permission(permission: str):
    async def dependency(
        principal: Annotated[Principal, Depends(get_principal)],
    ) -> Principal:
        if not AuthorizationService.can(principal, permission):
            raise HTTPException(status_code=403, detail="Permission denied")
        return principal

    return dependency
