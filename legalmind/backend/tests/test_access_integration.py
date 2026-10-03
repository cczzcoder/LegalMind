"""页面级授权集成测试（FR-10、设计 11.2）：需要 TEST_DATABASE_URL。"""

from uuid import UUID

import pytest
from sqlalchemy import select

from app.models import AuditEvent
from tests.helpers import create_page, login

pytestmark = pytest.mark.anyio


async def restricted_page(client) -> str:
    response = await create_page(client, access_scope="restricted")
    assert response.status_code == 201
    return response.json()["page_id"]


async def page_ids(client) -> set[str]:
    response = await client.get("/api/v1/wiki/pages")
    assert response.status_code == 200
    return {page["id"] for page in response.json()}


async def test_restricted_page_hidden_from_others(make_client, make_user):
    owner = await make_user("editor")
    other = await make_user("editor", organization_id=owner.organization_id)

    async with make_client() as owner_client, make_client() as other_client:
        await login(owner_client, owner.username)
        page_id = await restricted_page(owner_client)
        owner_list = await page_ids(owner_client)
        owner_history = await owner_client.get(f"/api/v1/wiki/pages/{page_id}/revisions")

        await login(other_client, other.username)
        other_list = await page_ids(other_client)
        history = await other_client.get(f"/api/v1/wiki/pages/{page_id}/revisions")
        edit = await other_client.post(
            f"/api/v1/wiki/pages/{page_id}/revisions",
            json={"expected_revision": 1, "body": "越权修改"},
        )
        missing = await other_client.get(
            "/api/v1/wiki/pages/00000000-0000-0000-0000-000000000000/revisions"
        )

    assert page_id in owner_list
    assert owner_history.status_code == 200
    assert page_id not in other_list
    # 无权访问与不存在不可区分
    assert history.status_code == edit.status_code == missing.status_code == 404
    assert history.json() == missing.json()


async def test_grant_and_revoke_take_effect_immediately(make_client, make_user):
    owner = await make_user("editor")
    reader = await make_user("reader", organization_id=owner.organization_id)
    grantor = await make_user("knowledge_admin", organization_id=owner.organization_id)

    async with (
        make_client() as owner_client,
        make_client() as reader_client,
        make_client() as grantor_client,
    ):
        await login(owner_client, owner.username)
        page_id = await restricted_page(owner_client)
        await login(reader_client, reader.username)
        await login(grantor_client, grantor.username)
        grant_url = f"/api/v1/wiki/pages/{page_id}/grants/{reader.id}"

        before = await reader_client.get(f"/api/v1/wiki/pages/{page_id}/revisions")
        granted = await grantor_client.put(grant_url)
        again = await grantor_client.put(grant_url)
        granted_read = await reader_client.get(f"/api/v1/wiki/pages/{page_id}/revisions")
        granted_list = await page_ids(reader_client)
        # 页面授权不扩大角色权限：reader 仍不能写
        granted_write = await reader_client.post(
            f"/api/v1/wiki/pages/{page_id}/revisions",
            json={"expected_revision": 1, "body": "读者修改"},
        )
        grants = await grantor_client.get(f"/api/v1/wiki/pages/{page_id}/grants")

        revoked = await grantor_client.delete(grant_url)
        revoked_again = await grantor_client.delete(grant_url)
        after = await reader_client.get(f"/api/v1/wiki/pages/{page_id}/revisions")

    assert before.status_code == 404
    assert granted.status_code == 200
    assert granted.json()["granted_by"] == str(grantor.id)
    assert again.status_code == 200
    assert again.json()["created_at"] == granted.json()["created_at"]
    assert granted_read.status_code == 200
    assert page_id in granted_list
    assert granted_write.status_code == 403
    assert {g["user_id"] for g in grants.json()} == {str(owner.id), str(reader.id)}
    assert revoked.status_code == 204
    assert revoked_again.status_code == 404
    assert after.status_code == 404


async def test_grantor_manages_without_reading(make_client, make_user):
    owner = await make_user("editor")
    grantor = await make_user("knowledge_admin", organization_id=owner.organization_id)

    async with make_client() as owner_client, make_client() as grantor_client:
        await login(owner_client, owner.username)
        page_id = await restricted_page(owner_client)
        await login(grantor_client, grantor.username)

        # 管理授权不等于可阅读内容
        read = await grantor_client.get(f"/api/v1/wiki/pages/{page_id}/revisions")
        grants = await grantor_client.get(f"/api/v1/wiki/pages/{page_id}/grants")
        opened = await grantor_client.put(
            f"/api/v1/wiki/pages/{page_id}/access", json={"access_scope": "organization"}
        )
        read_after_open = await grantor_client.get(f"/api/v1/wiki/pages/{page_id}/revisions")

    assert read.status_code == 404
    assert grants.status_code == 200
    assert opened.status_code == 200
    assert opened.json()["access_scope"] == "organization"
    assert read_after_open.status_code == 200


async def test_restricting_existing_page_hides_it(make_client, make_user):
    grantor = await make_user("knowledge_admin")
    editor = await make_user("editor", organization_id=grantor.organization_id)

    async with make_client() as grantor_client, make_client() as editor_client:
        await login(grantor_client, grantor.username)
        await login(editor_client, editor.username)
        created = await create_page(editor_client)
        page_id = created.json()["page_id"]
        before = await page_ids(editor_client)

        restricted = await grantor_client.put(
            f"/api/v1/wiki/pages/{page_id}/access", json={"access_scope": "restricted"}
        )
        after = await page_ids(editor_client)

    assert page_id in before
    assert restricted.json()["access_scope"] == "restricted"
    # 组织公开页面转为受限后，原作者也需要单独授权
    assert page_id not in after


async def test_grant_endpoints_require_grant_permission(make_client, make_user):
    owner = await make_user("editor")
    reader = await make_user("reader", organization_id=owner.organization_id)

    async with make_client() as client:
        await login(client, owner.username)
        page_id = await restricted_page(client)
        base = f"/api/v1/wiki/pages/{page_id}"
        responses = [
            await client.put(f"{base}/access", json={"access_scope": "organization"}),
            await client.get(f"{base}/grants"),
            await client.put(f"{base}/grants/{reader.id}"),
            await client.delete(f"{base}/grants/{owner.id}"),
        ]

    assert [r.status_code for r in responses] == [403] * 4


async def test_cross_organization_grants_are_rejected(make_client, make_user):
    grantor = await make_user("knowledge_admin")
    outsider = await make_user("editor")

    async with make_client() as grantor_client, make_client() as outsider_client:
        await login(grantor_client, grantor.username)
        own_page = await restricted_page(grantor_client)
        await login(outsider_client, outsider.username)
        foreign_page = await restricted_page(outsider_client)

        to_outsider = await grantor_client.put(
            f"/api/v1/wiki/pages/{own_page}/grants/{outsider.id}"
        )
        on_foreign = await grantor_client.put(
            f"/api/v1/wiki/pages/{foreign_page}/grants/{grantor.id}"
        )
        open_foreign = await grantor_client.put(
            f"/api/v1/wiki/pages/{foreign_page}/access", json={"access_scope": "organization"}
        )

    assert to_outsider.status_code == 404
    assert on_foreign.status_code == 404
    assert open_foreign.status_code == 404


async def test_access_changes_are_audited(make_client, make_user, session_factory):
    grantor = await make_user("knowledge_admin")
    reader = await make_user("reader", organization_id=grantor.organization_id)

    async with make_client() as client:
        await login(client, grantor.username)
        page_id = await restricted_page(client)
        base = f"/api/v1/wiki/pages/{page_id}"
        await client.put(f"{base}/grants/{reader.id}")
        await client.delete(f"{base}/grants/{reader.id}")
        await client.put(f"{base}/access", json={"access_scope": "organization"})

    async with session_factory() as session:
        events = list(
            await session.scalars(
                select(AuditEvent)
                .where(AuditEvent.resource_id == UUID(page_id))
                .order_by(AuditEvent.created_at)
            )
        )

    assert [e.action for e in events] == [
        "wiki.page.created",
        "wiki.page.access_granted",
        "wiki.page.access_revoked",
        "wiki.page.access_scope_changed",
    ]
    assert all(e.actor_id == grantor.id for e in events)
    assert events[3].payload == {
        "page_id": page_id,
        "before": "restricted",
        "after": "organization",
    }
