"""认证与授权集成测试（FR-10、设计 11.1/11.2）：需要 TEST_DATABASE_URL。"""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, select, update

from app.core.security import CSRF_HEADER, SESSION_COOKIE, hash_token
from app.models import AuditEvent, AuthSession, User
from app.modules.identity import service
from tests.helpers import PASSWORD, create_page, login

pytestmark = pytest.mark.anyio


async def test_login_sets_hardened_cookie_and_me_works(make_client, make_user):
    user = await make_user("editor")

    async with make_client() as client:
        response = await login(client, user.username)
        me = await client.get("/api/v1/auth/me")

    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert f"{SESSION_COOKIE}=" in cookie
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert me.status_code == 200
    assert me.json()["username"] == user.username
    assert me.json()["roles"] == ["editor"]
    assert me.json()["csrf_token"] == response.json()["csrf_token"]


async def test_wrong_password_and_unknown_user_look_the_same(make_client, make_user):
    user = await make_user("editor")

    async with make_client() as client:
        wrong = await login(client, user.username, "wrong password!!")
        unknown = await login(client, f"nobody-{uuid4().hex[:8]}")

    assert wrong.status_code == unknown.status_code == 401
    # trace_id 每个请求都不同，只比较与账号存在性相关的字段
    assert wrong.json()["code"] == unknown.json()["code"]
    assert wrong.json()["message"] == unknown.json()["message"]
    assert SESSION_COOKIE not in wrong.headers.get("set-cookie", "")


async def test_unauthenticated_and_forged_cookie_are_rejected(make_client):
    async with make_client() as client:
        no_cookie = await client.get("/api/v1/wiki/pages")
        client.cookies.set(SESSION_COOKIE, "forged-token")
        forged = await client.get("/api/v1/wiki/pages")

    assert no_cookie.status_code == 401
    assert forged.status_code == 401


async def test_write_requires_csrf_token(make_client, make_user):
    user = await make_user("editor")

    async with make_client() as client:
        await login(client, user.username)
        csrf = client.headers.pop(CSRF_HEADER)

        missing = await create_page(client)
        client.headers[CSRF_HEADER] = "x" * len(csrf)
        wrong = await create_page(client)
        client.headers[CSRF_HEADER] = csrf
        ok = await create_page(client)

    assert missing.status_code == 403
    assert wrong.status_code == 403
    assert ok.status_code == 201


async def test_role_permissions(make_client, make_user):
    reader = await make_user("reader")
    admin = await make_user("system_admin")

    async with make_client() as client:
        await login(client, reader.username)
        reader_list = await client.get("/api/v1/wiki/pages")
        reader_write = await create_page(client)
        reader_users = await client.get("/api/v1/users")

    async with make_client() as client:
        await login(client, admin.username)
        admin_users = await client.get("/api/v1/users")
        # 系统管理员不自动拥有业务内容权限
        admin_wiki = await client.get("/api/v1/wiki/pages")

    assert reader_list.status_code == 200
    assert reader_write.status_code == 403
    assert reader_users.status_code == 403
    assert admin_users.status_code == 200
    assert admin_wiki.status_code == 403


async def test_logout_revokes_session(make_client, make_user):
    user = await make_user("editor")

    async with make_client() as client:
        await login(client, user.username)
        token = client.cookies[SESSION_COOKIE]
        logout = await client.post("/api/v1/auth/logout")

    async with make_client() as replay:
        replay.cookies.set(SESSION_COOKIE, token)
        after = await replay.get("/api/v1/wiki/pages")

    assert logout.status_code == 204
    assert after.status_code == 401


async def test_disable_user_revokes_sessions_and_blocks_login(make_client, make_user):
    admin = await make_user("system_admin")
    editor = await make_user("editor", organization_id=admin.organization_id)

    async with make_client() as editor_client, make_client() as admin_client:
        await login(editor_client, editor.username)
        assert (await editor_client.get("/api/v1/wiki/pages")).status_code == 200

        await login(admin_client, admin.username)
        disabled = await admin_client.post(f"/api/v1/users/{editor.id}/disable")

        after = await editor_client.get("/api/v1/wiki/pages")
        relogin = await login(editor_client, editor.username)

        enabled = await admin_client.post(f"/api/v1/users/{editor.id}/enable")
        relogin_after_enable = await login(editor_client, editor.username)

    assert disabled.status_code == 200
    assert disabled.json()["is_active"] is False
    assert after.status_code == 401
    assert relogin.status_code == 401
    assert enabled.status_code == 200
    assert relogin_after_enable.status_code == 200


async def test_inactive_user_session_rejected_even_if_not_revoked(
    make_client, make_user, session_factory
):
    # 绕过禁用接口（它会同时吊销会话），单独验证每次请求都检查 is_active
    user = await make_user("editor")

    async with make_client() as client:
        await login(client, user.username)
        async with session_factory() as session, session.begin():
            await session.execute(update(User).where(User.id == user.id).values(is_active=False))
        after = await client.get("/api/v1/wiki/pages")

    assert after.status_code == 401


async def test_role_change_applies_to_existing_session(make_client, make_user):
    admin = await make_user("system_admin")
    user = await make_user("reader", organization_id=admin.organization_id)

    async with make_client() as user_client, make_client() as admin_client:
        await login(user_client, user.username)
        before = await create_page(user_client)

        await login(admin_client, admin.username)
        changed = await admin_client.put(
            f"/api/v1/users/{user.id}/roles",
            json={"roles": ["editor"]},
        )
        after = await create_page(user_client)

        bad_role = await admin_client.put(
            f"/api/v1/users/{user.id}/roles",
            json={"roles": ["superuser"]},
        )

    assert before.status_code == 403
    assert changed.status_code == 200
    assert changed.json()["roles"] == ["editor"]
    assert after.status_code == 201
    assert bad_role.status_code == 422


async def test_admin_cannot_manage_other_organization_users(make_client, make_user):
    admin = await make_user("system_admin")
    outsider = await make_user("editor")

    async with make_client() as client:
        await login(client, admin.username)
        listed = await client.get("/api/v1/users")
        disable = await client.post(f"/api/v1/users/{outsider.id}/disable")
        roles = await client.put(
            f"/api/v1/users/{outsider.id}/roles",
            json={"roles": ["reader"]},
        )

    assert outsider.id not in {u["id"] for u in listed.json()}
    assert disable.status_code == 404
    assert roles.status_code == 404


async def test_list_users_returns_roles_without_n_plus_one(make_client, make_user, engine):
    admin = await make_user("system_admin")
    organization_id = admin.organization_id
    readers = [await make_user("reader", organization_id=organization_id) for _ in range(5)]

    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        async with make_client() as client:
            await login(client, admin.username)
            statements.clear()
            response = await client.get("/api/v1/users")
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)

    assert response.status_code == 200
    # 角色查询应为常数：1 次鉴权（core/security.py 每请求重读当前用户角色）
    # + 1 次列表批量查询；N+1 写法在 6 个用户下会变成 1+6=7（回归保护）
    assert sum("user_roles" in statement for statement in statements) == 2
    roles_by_id = {user["id"]: user["roles"] for user in response.json()}
    assert roles_by_id[str(admin.id)] == ["system_admin"]
    for reader in readers:
        assert roles_by_id[str(reader.id)] == ["reader"]


async def test_admin_cannot_lock_themselves_out(make_client, make_user):
    admin = await make_user("system_admin")

    async with make_client() as client:
        await login(client, admin.username)
        disable = await client.post(f"/api/v1/users/{admin.id}/disable")
        demote = await client.put(
            f"/api/v1/users/{admin.id}/roles",
            json={"roles": ["reader"]},
        )

    assert disable.status_code == 400
    assert demote.status_code == 400


async def test_repeated_failures_lock_username(make_client, make_user):
    user = await make_user("editor")

    async with make_client() as client:
        failures = [
            (await login(client, user.username, "wrong password!!")).status_code
            for _ in range(service.MAX_FAILURES_PER_USERNAME)
        ]
    # 换一个 IP、用正确密码仍被锁定：按用户名计数
    async with make_client() as client:
        locked = await login(client, user.username)

    assert failures == [401] * service.MAX_FAILURES_PER_USERNAME
    assert locked.status_code == 429


async def test_idle_session_expires(make_client, make_user, session_factory):
    user = await make_user("editor")

    async with make_client() as client:
        await login(client, user.username)
        token = client.cookies[SESSION_COOKIE]

        async with session_factory() as session, session.begin():
            await session.execute(
                update(AuthSession)
                .where(AuthSession.token_hash == hash_token(token))
                .values(last_seen_at=AuthSession.last_seen_at - timedelta(hours=1))
            )

        expired = await client.get("/api/v1/wiki/pages")

    assert expired.status_code == 401


async def test_auth_events_are_audited_without_secrets(make_client, make_user, session_factory):
    user = await make_user("editor")

    async with make_client() as client:
        await login(client, user.username, "wrong password!!")
        await login(client, user.username)
        csrf = client.headers[CSRF_HEADER]
        token = client.cookies[SESSION_COOKIE]
        await client.post("/api/v1/auth/logout")

    async with session_factory() as session:
        events = list(
            await session.scalars(
                select(AuditEvent)
                .where(AuditEvent.resource_id == user.id)
                .order_by(AuditEvent.created_at)
            )
        )

    assert [e.action for e in events] == [
        "user.created",
        "auth.login.failed",
        "auth.login.succeeded",
        "auth.logout",
    ]
    assert events[1].actor_id is None
    dumped = str([e.payload for e in events])
    for secret in (PASSWORD, "wrong password!!", csrf, token):
        assert secret not in dumped
