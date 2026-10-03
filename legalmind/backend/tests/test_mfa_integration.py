"""管理员 TOTP 第二因素集成测试（FR-10、设计 11.1）：需要 TEST_DATABASE_URL。"""

import pyotp
import pytest
from sqlalchemy import select

from app.models import AuditEvent, MfaRecoveryCode, User
from app.modules.identity import mfa
from tests.helpers import AUTHENTICATORS, enroll_mfa, login, next_totp

pytestmark = pytest.mark.anyio


async def test_admin_must_enroll_before_using_business_api(make_client, make_user):
    admin = await make_user("system_admin")

    async with make_client() as client:
        response = await login(client, admin.username, complete_mfa=False)
        me = await client.get("/api/v1/auth/me")
        blocked = await client.get("/api/v1/users")

        codes = await enroll_mfa(client, admin.username)
        allowed = await client.get("/api/v1/users")
        me_after = await client.get("/api/v1/auth/me")

    assert response.json()["mfa_status"] == "enroll"
    assert me.status_code == 200
    assert me.json()["mfa_status"] == "enroll"
    assert blocked.status_code == 403
    assert len(codes) == mfa.RECOVERY_CODE_COUNT
    assert allowed.status_code == 200
    assert me_after.json()["mfa_status"] == "ok"
    assert me_after.json()["mfa_enabled"] is True


async def test_non_admin_does_not_need_mfa(make_client, make_user):
    editor = await make_user("editor")

    async with make_client() as client:
        response = await login(client, editor.username, complete_mfa=False)
        pages = await client.get("/api/v1/wiki/pages")

    assert response.json()["mfa_status"] == "ok"
    assert pages.status_code == 200


async def test_new_session_requires_verification(make_client, make_user):
    admin = await make_user("knowledge_admin")

    async with make_client() as client:
        await login(client, admin.username)

    async with make_client() as client:
        response = await login(client, admin.username, complete_mfa=False)
        blocked = await client.get("/api/v1/wiki/pages")
        wrong = await client.post("/api/v1/auth/mfa/verify", json={"code": "000000"})
        ok = await client.post("/api/v1/auth/mfa/verify", json={"code": next_totp(admin.username)})
        allowed = await client.get("/api/v1/wiki/pages")

    assert response.json()["mfa_status"] == "verify"
    assert blocked.status_code == 403
    assert wrong.status_code == 400
    assert ok.status_code == 204
    assert allowed.status_code == 200


async def test_totp_code_cannot_be_replayed(make_client, make_user):
    admin = await make_user("system_admin")

    async with make_client() as client:
        await login(client, admin.username)
    code = next_totp(admin.username)

    async with make_client() as first, make_client() as second:
        await login(first, admin.username, complete_mfa=False)
        await login(second, admin.username, complete_mfa=False)
        used = await first.post("/api/v1/auth/mfa/verify", json={"code": code})
        replay = await second.post("/api/v1/auth/mfa/verify", json={"code": code})

    assert used.status_code == 204
    assert replay.status_code == 400


async def test_recovery_code_works_once(make_client, make_user):
    admin = await make_user("system_admin")

    async with make_client() as client:
        await login(client, admin.username, complete_mfa=False)
        codes = await enroll_mfa(client, admin.username)

    async with make_client() as first, make_client() as second:
        await login(first, admin.username, complete_mfa=False)
        await login(second, admin.username, complete_mfa=False)
        # 用户可能输入大写或去掉连字符
        used = await first.post(
            "/api/v1/auth/mfa/verify", json={"code": codes[0].upper().replace("-", "")}
        )
        reused = await second.post("/api/v1/auth/mfa/verify", json={"code": codes[0]})
        other = await second.post("/api/v1/auth/mfa/verify", json={"code": codes[1]})

    assert used.status_code == 204
    assert reused.status_code == 400
    assert other.status_code == 204


async def test_repeated_mfa_failures_are_throttled(make_client, make_user):
    admin = await make_user("system_admin")

    async with make_client() as client:
        await login(client, admin.username)

    async with make_client() as client:
        await login(client, admin.username, complete_mfa=False)
        failures = [
            (await client.post("/api/v1/auth/mfa/verify", json={"code": "000000"})).status_code
            for _ in range(mfa.MAX_MFA_FAILURES)
        ]
        # 达到上限后即使验证码正确也拒绝
        locked = await client.post(
            "/api/v1/auth/mfa/verify", json={"code": next_totp(admin.username)}
        )

    assert failures == [400] * mfa.MAX_MFA_FAILURES
    assert locked.status_code == 429


async def test_enrollment_cannot_be_restarted_once_enabled(make_client, make_user):
    admin = await make_user("system_admin")

    async with make_client() as client:
        await login(client, admin.username)
        again = await client.post("/api/v1/auth/mfa/enroll")

    assert again.status_code == 409


async def test_secrets_are_not_stored_in_plaintext(make_client, make_user, session_factory):
    admin = await make_user("system_admin")

    async with make_client() as client:
        await login(client, admin.username, complete_mfa=False)
        codes = await enroll_mfa(client, admin.username)
    secret = AUTHENTICATORS[admin.username][0]

    async with session_factory() as session:
        user = await session.get(User, admin.id)
        hashes = list(
            await session.scalars(
                select(MfaRecoveryCode.code_hash).where(MfaRecoveryCode.user_id == admin.id)
            )
        )
        events = list(
            await session.scalars(select(AuditEvent).where(AuditEvent.resource_id == admin.id))
        )

    assert secret not in user.mfa_secret_encrypted
    assert pyotp.TOTP(mfa.decrypt_secret(user)).secret == secret
    dumped = str(hashes) + str([e.payload for e in events])
    for value in [secret, *codes, *(c.replace("-", "") for c in codes)]:
        assert value not in dumped
    assert "user.mfa.enabled" in {e.action for e in events}


async def test_reset_clears_mfa_and_revokes_sessions(make_client, make_user, session_factory):
    admin = await make_user("system_admin")

    async with make_client() as client:
        await login(client, admin.username)
        async with session_factory() as session:
            await mfa.reset(session, admin.username)
        after = await client.get("/api/v1/users")

    async with make_client() as client:
        relogin = await login(client, admin.username, complete_mfa=False)

    assert after.status_code == 401
    assert relogin.json()["mfa_status"] == "enroll"
