from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_session
from app.core.security import (
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    Principal,
    get_principal,
)
from app.models import AuthSession, User
from app.modules.authorization.service import USER_MANAGE, require_permission
from app.modules.identity import service
from app.modules.identity.schemas import (
    CreateUser,
    CurrentUser,
    LoginRequest,
    SetRoles,
    UserOut,
)

router = APIRouter(tags=["identity"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
PrincipalDep = Annotated[Principal, Depends(get_principal)]
AdminDep = Annotated[Principal, Depends(require_permission(USER_MANAGE))]


@router.post("/auth/login", response_model=CurrentUser)
async def login(
    data: LoginRequest,
    request: Request,
    response: Response,
    session: SessionDep,
):
    # 直连时取对端地址；部署反向代理后需配置可信代理头（3b）
    ip_address = request.client.host if request.client else "unknown"
    result = await service.login(session, data.username, data.password, ip_address)

    response.set_cookie(
        SESSION_COOKIE,
        result.token,
        max_age=int(SESSION_MAX_AGE.total_seconds()),
        httponly=True,
        secure=get_settings().session_cookie_secure,
        samesite="lax",
        path="/",
    )
    return CurrentUser(**result.user.model_dump(), csrf_token=result.csrf_token)


@router.post("/auth/logout", status_code=204)
async def logout(
    response: Response,
    session: SessionDep,
    principal: PrincipalDep,
):
    await service.logout(session, principal)
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get("/auth/me", response_model=CurrentUser)
async def me(session: SessionDep, principal: PrincipalDep):
    # 前端刷新页面后从这里取回 CSRF 令牌；GET 不改变状态
    user = await session.get(User, principal.user_id)
    csrf_token = await session.scalar(
        select(AuthSession.csrf_token).where(AuthSession.id == principal.session_id)
    )
    out = await service.user_out(session, user)
    return CurrentUser(**out.model_dump(), csrf_token=csrf_token)


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
