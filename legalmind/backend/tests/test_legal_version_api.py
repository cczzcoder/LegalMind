"""法律版本复核接口（需求第 3 节「法律审核员：审核资料版本」；设计 §7、§11.2、§13）。

复核的服务层逻辑在 `test_legal_version_review.py` 里钉；这里钉**接口这一层**：
两种模式的权限差异、授权仍在数据库里复核（§11.2）、以及状态码（404 / 409 / 422）。
"""

from uuid import uuid4

import pytest

from app.models import LegalInstrument, LegalVersion
from tests.helpers import import_document_into_source, login, setup_source

pytestmark = pytest.mark.anyio


async def _sign_in(client, make_user, *roles: str, **kwargs):
    """登录**已经打开**的客户端（顺序不能反，见 `test_answering_api.py` 的说明）。"""
    user = await make_user(*(roles or ("knowledge_admin",)), **kwargs)
    await login(client, user.username)
    return user


async def _land_version(session_factory, user, *, artifact_id=None, review_status="pending"):
    async with session_factory() as session, session.begin():
        instrument = LegalInstrument(
            title=f"示例测试法（{uuid4().hex[:8]}）",
            jurisdiction="中国",
            issuing_body="全国人民代表大会常务委员会",
            instrument_type="law",
            created_by=user.id,
        )
        session.add(instrument)
        await session.flush()
        version = LegalVersion(
            instrument_id=instrument.id,
            artifact_id=artifact_id,
            version_label="2026年修订",
            legal_status="repealed",
            review_status=review_status,
            created_by=user.id,
        )
        session.add(version)
    return instrument.id, version.id


async def test_listing_needs_only_document_read(make_client, make_user, session_factory):
    """法律版本是**公共法律数据**，能读资料的登录用户就该看得到清单。"""
    _, version_id = await _land_version(session_factory, await make_user("knowledge_admin"))
    async with make_client() as client:
        await _sign_in(client, make_user, "reader")
        response = await client.get("/api/v1/legal-versions")
    assert response.status_code == 200, response.text
    # ⚠️ 接口里的 id 是**字符串**（JSON），拿 UUID 去比永远不相等——那样断言会变成空转
    assert str(version_id) in [row["id"] for row in response.json()]


async def test_pending_queue_needs_review_permission(make_client, make_user, session_factory):
    """「哪些数据还没被人确认」是审核面的信息——与 `GET /answers?pending=true` 同一条口径。"""
    await _land_version(session_factory, await make_user("knowledge_admin"))

    async with make_client() as client:
        await _sign_in(client, make_user, "reader")
        refused = await client.get("/api/v1/legal-versions", params={"pending": "true"})
    assert refused.status_code == 403

    async with make_client() as client:
        await _sign_in(client, make_user, "legal_reviewer")
        allowed = await client.get("/api/v1/legal-versions", params={"pending": "true"})
    assert allowed.status_code == 200
    assert all(row["review_status"] == "pending" for row in allowed.json())


async def test_review_approves_and_returns_display_fields(make_client, make_user, session_factory):
    """复核后要回传展示字段——页面上还要接着看，只回一个 id 没法渲染。"""
    _, version_id = await _land_version(session_factory, await make_user("knowledge_admin"))

    async with make_client() as client:
        await _sign_in(client, make_user, "legal_reviewer")
        response = await client.post(
            f"/api/v1/legal-versions/{version_id}/review",
            json={"decision": "approved", "note": "公布信息完整"},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["review_status"] == "approved"
    assert body["instrument_title"].startswith("示例测试法")
    assert body["version_label"] == "2026年修订"


async def test_review_never_changes_the_legal_status(make_client, make_user, session_factory):
    """⚠️ 安全不变量：复核是**数据质量意见**，不是法律效力判断。

    `legal_status` 由正文前言的公布信息推出；把「已废止」点成「现行有效」正是 §8.3 要防的事。
    """
    _, version_id = await _land_version(session_factory, await make_user("knowledge_admin"))

    async with make_client() as client:
        await _sign_in(client, make_user, "legal_reviewer")
        response = await client.post(
            f"/api/v1/legal-versions/{version_id}/review", json={"decision": "approved"}
        )
    assert response.status_code == 200
    assert response.json()["legal_status"] == "repealed"

    async with session_factory() as session:
        stored = await session.get(LegalVersion, version_id)
    assert stored.legal_status == "repealed"
    assert stored.review_status == "approved"


async def test_review_needs_review_permission(make_client, make_user, session_factory):
    _, version_id = await _land_version(session_factory, await make_user("knowledge_admin"))
    async with make_client() as client:
        await _sign_in(client, make_user, "reader")
        response = await client.post(
            f"/api/v1/legal-versions/{version_id}/review", json={"decision": "approved"}
        )
    assert response.status_code == 403


async def test_repeating_the_same_decision_is_a_conflict(make_client, make_user, session_factory):
    _, version_id = await _land_version(session_factory, await make_user("knowledge_admin"))
    async with make_client() as client:
        await _sign_in(client, make_user, "legal_reviewer")
        first = await client.post(
            f"/api/v1/legal-versions/{version_id}/review", json={"decision": "approved"}
        )
        again = await client.post(
            f"/api/v1/legal-versions/{version_id}/review", json={"decision": "approved"}
        )
    assert first.status_code == 200
    assert again.status_code == 409, again.text


async def test_unknown_version_is_404(make_client, make_user):
    async with make_client() as client:
        await _sign_in(client, make_user, "legal_reviewer")
        response = await client.post(
            f"/api/v1/legal-versions/{uuid4()}/review", json={"decision": "approved"}
        )
    assert response.status_code == 404


async def test_pending_is_not_an_acceptable_decision(make_client, make_user, session_factory):
    """`pending` 是「机器没把握」，不是人能做出的结论——请求体层面就该挡掉。"""
    _, version_id = await _land_version(session_factory, await make_user("knowledge_admin"))
    async with make_client() as client:
        await _sign_in(client, make_user, "legal_reviewer")
        response = await client.post(
            f"/api/v1/legal-versions/{version_id}/review", json={"decision": "pending"}
        )
    assert response.status_code == 422


async def test_a_restricted_artifact_hides_the_version_without_a_grant(
    make_client, make_user, session_factory
):
    """授权照旧在数据库里复核（§11.2）：受限原件没授权，连版本清单里都不该出现。

    与 `test_retrieval_integration.py::test_restricted_document_is_only_visible_with_a_grant`
    同一形状——「列个版本清单」不该成为绕过授权的一条旁路。
    """
    admin, editor, source_id = await setup_source(make_client, make_user)
    outsider = await make_user("reader", organization_id=admin.organization_id)

    artifact_id = await import_document_into_source(
        make_client,
        editor,
        source_id,
        # ⚠️ 内容必须每次不同：原件按 sha256 全库唯一，测试库又是跨运行累积的
        f"placeholder-{uuid4()}".encode(),
        "受限示例.txt",
        access_scope="restricted",
    )
    _, version_id = await _land_version(session_factory, editor, artifact_id=artifact_id)

    async with make_client() as client:
        await login(client, outsider.username)
        hidden = await client.get("/api/v1/legal-versions")
    assert hidden.status_code == 200
    assert str(version_id) not in [row["id"] for row in hidden.json()], "没授权就不该看到"

    # 授权后即可见（knowledge_admin 管理授权）
    async with make_client() as client:
        await login(client, admin.username)
        granted = await client.put(f"/api/v1/documents/{artifact_id}/grants/{outsider.id}")
    assert granted.status_code == 200, granted.text

    async with make_client() as client:
        await login(client, outsider.username)
        visible = await client.get("/api/v1/legal-versions")
    assert str(version_id) in [row["id"] for row in visible.json()], "授权之后应当可见"
