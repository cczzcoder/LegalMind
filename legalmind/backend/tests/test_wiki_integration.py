"""Wiki 集成测试：需要真实 PostgreSQL，通过 TEST_DATABASE_URL 指定（未设置时跳过）。"""

import asyncio
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

from app.core.database import get_session
from app.core.security import Principal, get_principal
from app.main import app
from app.models import AuditEvent, OutboxEvent, WikiPage, WikiRevision
from app.modules.wiki import service
from app.modules.wiki.schemas import CreatePage, CreateRevision

pytestmark = pytest.mark.anyio


def new_principal() -> Principal:
    # 每个测试使用随机组织，测试之间互不干扰
    return Principal(organization_id=uuid4(), user_id=uuid4())


async def count(session_factory, model, organization_id: UUID) -> int:
    async with session_factory() as session:
        return await session.scalar(
            select(func.count())
            .select_from(model)
            .where(model.organization_id == organization_id)
        )


@pytest.fixture
def client_as(session_factory):
    """返回一个以指定 Principal 身份访问 API 的客户端工厂。"""

    async def override_session():
        async with session_factory() as session:
            yield session

    def make(principal: Principal) -> httpx.AsyncClient:
        app.dependency_overrides[get_session] = override_session
        app.dependency_overrides[get_principal] = lambda: principal
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        )

    yield make
    app.dependency_overrides.clear()


async def test_other_organization_cannot_see_or_edit_page(client_as):
    owner, outsider = new_principal(), new_principal()

    async with client_as(owner) as client:
        created = await client.post(
            "/api/v1/wiki/pages",
            json={"title": "合同解除", "body": "第一版"},
        )
    assert created.status_code == 201
    page_id = created.json()["page_id"]

    async with client_as(outsider) as client:
        listed = await client.get("/api/v1/wiki/pages")
        history = await client.get(f"/api/v1/wiki/pages/{page_id}/revisions")
        edit = await client.post(
            f"/api/v1/wiki/pages/{page_id}/revisions",
            json={"expected_revision": 1, "body": "越权修改"},
        )

    assert listed.status_code == 200
    assert page_id not in {page["id"] for page in listed.json()}
    # 不存在与无权访问返回同样的 404，不泄露页面存在性
    assert history.status_code == 404
    assert edit.status_code == 404

    async with client_as(owner) as client:
        history = await client.get(f"/api/v1/wiki/pages/{page_id}/revisions")
    assert [rev["number"] for rev in history.json()] == [1]


async def test_outbox_failure_rolls_back_page_and_audit(session_factory, monkeypatch):
    principal = new_principal()

    def broken_outbox_event(**kwargs):
        # event_type 超过 VARCHAR(100)，在数据库层写入失败
        return OutboxEvent(**{**kwargs, "event_type": "x" * 101})

    monkeypatch.setattr(service, "OutboxEvent", broken_outbox_event)

    async with session_factory() as session:
        with pytest.raises(DBAPIError):
            await service.create_page(
                session,
                principal,
                CreatePage(title="回滚测试", body="正文"),
            )

    assert await count(session_factory, WikiPage, principal.organization_id) == 0
    assert await count(session_factory, AuditEvent, principal.organization_id) == 0
    assert await count(session_factory, OutboxEvent, principal.organization_id) == 0


async def test_concurrent_edits_on_same_revision_only_one_wins(session_factory):
    principal = new_principal()

    async with session_factory() as session:
        first = await service.create_page(
            session,
            principal,
            CreatePage(title="并发测试", body="第一版"),
        )
    page_id = first.page_id

    async def edit(body: str):
        async with session_factory() as session:
            return await service.create_revision(
                session,
                principal,
                page_id,
                CreateRevision(expected_revision=1, body=body),
            )

    # 先持有页面行锁，让两个编辑都卡在 FOR UPDATE 上，再同时放行
    async with session_factory() as holder, holder.begin():
        await holder.execute(
            select(WikiPage).where(WikiPage.id == page_id).with_for_update()
        )
        tasks = [
            asyncio.create_task(edit("编辑 A")),
            asyncio.create_task(edit("编辑 B")),
        ]
        await asyncio.sleep(0.3)
        assert not any(task.done() for task in tasks)

    results = await asyncio.gather(*tasks, return_exceptions=True)

    succeeded = [r for r in results if isinstance(r, WikiRevision)]
    conflicts = [
        r for r in results if isinstance(r, HTTPException) and r.status_code == 409
    ]
    assert len(succeeded) == 1
    assert len(conflicts) == 1
    assert succeeded[0].number == 2

    async with session_factory() as session:
        page = await session.get(WikiPage, page_id)
        numbers = await session.scalars(
            select(WikiRevision.number)
            .where(WikiRevision.page_id == page_id)
            .order_by(WikiRevision.number)
        )
        assert page.head_revision == 2
        assert list(numbers) == [1, 2]

    # 失败的编辑不留审计和 Outbox：页面创建 1 条 + 成功修订 1 条
    assert await count(session_factory, AuditEvent, principal.organization_id) == 2
    assert await count(session_factory, OutboxEvent, principal.organization_id) == 2
