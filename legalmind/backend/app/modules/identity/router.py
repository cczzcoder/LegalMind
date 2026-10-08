from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_session
from app.core.security import (
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    Principal,
    client_ip,
    get_session_principal,
    mfa_status,
)
from app.models import AuthSession, User
from app.modules.authorization.service import (
    DOCUMENT_GRANT,
    USER_MANAGE,
    WIKI_GRANT,
    AuthorizationService,
    permissions_for,
    require_permission,
)
from app.modules.identity import mfa, service
from app.modules.identity.schemas import (
    CreateUser,
    CurrentUser,
    DirectoryEntry,
    LoginRequest,
    MfaCode,
    MfaEnrollment,
    RecoveryCodes,
    SetRoles,
    UserOut,
)

router = APIRouter(tags=["identity"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
# 只要求已登录：第二因素未完成时也能退出、查看状态和完成 MFA
LoggedInDep = Annotated[Principal, Depends(get_session_principal)]
AdminDep = Annotated[Principal, Depends(require_permission(USER_MANAGE))]

#: 能列「可授权对象」的权限：**授权人**就该看得到这份名单，不必是用户管理员。
GRANT_PERMISSIONS = (DOCUMENT_GRANT, WIKI_GRANT)


async def _grantor(principal: Annotated[Principal, Depends(get_session_principal)]) -> Principal:
    """任一「可授权」权限即可。

    `document.grant` 与 `wiki.grant` 是两件事，但**挑人的需求一样**——两处各开一个名单接口
    只会让「谁能被授权」出现两种口径。`require_permission` 只收单个权限，所以这里手写。
    """
    if not any(AuthorizationService.can(principal, permission) for permission in GRANT_PERMISSIONS):
        raise HTTPException(status_code=403, detail="Permission denied")
    return principal


GrantorDep = Annotated[Principal, Depends(_grantor)]


@router.post("/auth/login", response_model=CurrentUser)
async def login(
    data: LoginRequest,
    request: Request,
    response: Response,
    session: SessionDep,
):
    result = await service.login(session, data.username, data.password, client_ip(request))

    response.set_cookie(
        SESSION_COOKIE,
        result.token,
        max_age=int(SESSION_MAX_AGE.total_seconds()),
        httponly=True,
        secure=get_settings().session_cookie_secure,
        samesite="lax",
        path="/",
    )
    return CurrentUser(
        **result.user.model_dump(),
        csrf_token=result.csrf_token,
        permissions=sorted(permissions_for(result.user.roles)),
        mfa_status=result.mfa_status,
    )


@router.post("/auth/logout", status_code=204)
async def logout(
    response: Response,
    session: SessionDep,
    principal: LoggedInDep,
):
    await service.logout(session, principal)
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get("/auth/me", response_model=CurrentUser)
async def me(session: SessionDep, principal: LoggedInDep):
    # 前端刷新页面后从这里取回 CSRF 令牌；GET 不改变状态
    user = await session.get(User, principal.user_id)
    csrf_token = await session.scalar(
        select(AuthSession.csrf_token).where(AuthSession.id == principal.session_id)
    )
    out = await service.user_out(session, user)
    return CurrentUser(
        **out.model_dump(),
        csrf_token=csrf_token,
        permissions=sorted(permissions_for(out.roles)),
        mfa_status=mfa_status(user, principal.mfa_pending),
    )


@router.post("/auth/mfa/enroll", response_model=MfaEnrollment)
async def mfa_enroll(session: SessionDep, principal: LoggedInDep):
    # 密钥只在此响应中出现一次，用于导入认证器
    enrollment = await mfa.start_enrollment(session, principal)
    return MfaEnrollment(secret=enrollment.secret, otpauth_uri=enrollment.otpauth_uri)


@router.post("/auth/mfa/confirm", response_model=RecoveryCodes)
async def mfa_confirm(data: MfaCode, session: SessionDep, principal: LoggedInDep):
    # 恢复码只在此响应中出现一次
    codes = await mfa.confirm_enrollment(session, principal, data.code)
    return RecoveryCodes(recovery_codes=codes)


@router.post("/auth/mfa/verify", status_code=204)
async def mfa_verify(data: MfaCode, session: SessionDep, principal: LoggedInDep):
    await mfa.verify(session, principal, data.code)


@router.get("/users/directory", response_model=list[DirectoryEntry])
async def list_directory(session: SessionDep, principal: GrantorDep):
    """**可授权对象名单**（需求第 3 节「授权管理」）——授权人挑人用的最小信息。

    ⚠️ **为什么不能直接用 `GET /users`**：那个要 `user.manage`，只有 `system_admin` 有；
    而 `document.grant` / `wiki.grant` 在 `knowledge_admin` 手里——**有授权权的人列不出用户，
    就只能靠粘贴 UUID 发授权**，那等于把授权这件事挡在了门外。这里补的正是这一段。

    ⚠️ **刻意的窄**：只有 id / 用户名 / 是否启用，不含角色与 MFA 状态；范围限本组织（§21）。
    ⚠️ **`is_active` 要给**：给一个已停用的人发授权是白费功夫，名单上得看得出来。
    """
    return [
        DirectoryEntry(id=user.id, username=user.username, is_active=user.is_active)
        for user in await session.scalars(
            select(User)
            .where(User.organization_id == principal.organization_id)
            .order_by(User.username)
        )
    ]


@router.get("/users", response_model=list[UserOut])
async def list_users(session: SessionDep, principal: AdminDep):
    return await service.list_users(session, principal)


@router.post("/users", response_model=UserOut, status_code=201)
async def create_user(data: CreateUser, session: SessionDep, principal: AdminDep):
    return await service.create_user(
        session,
        principal.organization_id,
        principal.user_id,
        data,
    )


@router.put("/users/{user_id}/roles", response_model=UserOut)
async def set_roles(
    user_id: UUID,
    data: SetRoles,
    session: SessionDep,
    principal: AdminDep,
):
    return await service.set_roles(session, principal, user_id, data.roles)


@router.post("/users/{user_id}/disable", response_model=UserOut)
async def disable_user(user_id: UUID, session: SessionDep, principal: AdminDep):
    return await service.set_active(session, principal, user_id, False)


@router.post("/users/{user_id}/enable", response_model=UserOut)
async def enable_user(user_id: UUID, session: SessionDep, principal: AdminDep):
    return await service.set_active(session, principal, user_id, True)
