"""角色 × 接口权限矩阵（FR-10、设计 11.2）：需要 TEST_DATABASE_URL。

每个角色使用真实登录会话（含 MFA），期望值与 ROLE_PERMISSIONS 独立书写，
角色映射被误改时这里会失败。
"""

from uuid import uuid4

import pytest

from app.adapters.storage import LocalFileStorage, get_storage
from app.modules.authorization.service import ROLE_PERMISSIONS
from tests.helpers import create_page, login

pytestmark = pytest.mark.anyio

# 列顺序与 test_role_permission_matrix 中的请求顺序一致
COLUMNS = (
    "read pages",
    "write page",
    "manage page grants",
    "manage users",
    "read documents",
    "download document",
    "import document",
    "manage document grants",
    "create source",
)

EXPECTED = {
    "reader": (1, 0, 0, 0, 1, 1, 0, 0, 0),
    "editor": (1, 1, 0, 0, 1, 1, 1, 0, 0),
    "legal_reviewer": (1, 0, 0, 0, 1, 1, 0, 0, 0),
    "knowledge_admin": (1, 1, 1, 0, 1, 1, 1, 1, 1),
    "system_admin": (0, 0, 0, 1, 0, 0, 0, 0, 0),
    # V1.15 起审计人员要看审计与追溯记录（需求第 3 节），所以给了读页面与读资料；
    # **不含原件下载**——核对记录不等于取走原件
    "auditor": (1, 0, 0, 0, 1, 0, 0, 0, 0),
}


def test_matrix_covers_every_role():
    assert set(EXPECTED) == set(ROLE_PERMISSIONS)
    assert all(len(row) == len(COLUMNS) for row in EXPECTED.values())


async def create_source(client, name: str):
    return await client.post(
        "/api/v1/sources",
        json={
            # 来源属公共法律数据，名称全库唯一（设计 21.2）；测试用唯一名避免互相冲突
            "name": f"{name}-{uuid4().hex[:8]}",
            "source_type": "official",
            "trust_level": "high",
            "license_note": "测试",
        },
    )


async def import_document(client, source_id: str, content: bytes):
    # 原件按 sha256 全库唯一（设计 21.2），测试内容加唯一后缀避免互相冲突
    return await client.post(
        "/api/v1/documents",
        params={"source_id": source_id, "filename": "a.txt", "sensitivity": "public"},
        content=content + uuid4().hex.encode(),
    )


@pytest.mark.parametrize("role", sorted(EXPECTED))
async def test_role_permission_matrix(role, make_client, make_user, tmp_path):
    from app.main import app

    storage = LocalFileStorage(tmp_path)
    app.dependency_overrides[get_storage] = lambda: storage

    admin = await make_user("knowledge_admin")
    user = await make_user(role, organization_id=admin.organization_id)

    async with make_client() as admin_client:
        await login(admin_client, admin.username)
        page_id = (await create_page(admin_client)).json()["page_id"]
        source_id = (await create_source(admin_client, "来源")).json()["id"]
        imported = await import_document(admin_client, source_id, b"admin")
        document_id = imported.json()["document"]["id"]

    async with make_client() as client:
        await login(client, user.username)
        responses = (
            await client.get("/api/v1/wiki/pages"),
            await create_page(client),
            await client.get(f"/api/v1/wiki/pages/{page_id}/grants"),
            await client.get("/api/v1/users"),
            await client.get("/api/v1/documents"),
            await client.get(f"/api/v1/documents/{document_id}/content"),
            await import_document(client, source_id, role.encode()),
            await client.get(f"/api/v1/documents/{document_id}/grants"),
            await create_source(client, f"来源-{role}"),
        )

    allowed = tuple(int(r.status_code in (200, 201, 202)) for r in responses)
    assert dict(zip(COLUMNS, allowed, strict=True)) == dict(
        zip(COLUMNS, EXPECTED[role], strict=True)
    )
    # 拒绝必须是 403，而不是其他错误碰巧被当成拒绝
    for response, ok in zip(responses, allowed, strict=True):
        if not ok:
            assert response.status_code == 403


async def test_login_and_me_expose_the_permission_set(make_client, make_user):
    """`/auth/login` 与 `/auth/me` 都要回传**权限集合**（`permissions`）。

    这是前端显示层的依据：**前端不复制「角色→权限」映射**——那份复制会静默漂移
    （CODE_REVIEW m6：后端改了权限，前端的提示还停在旧名单）。所以后端把集合算好给出去，
    并由这条测试钉住「接口回传的集合 == `permissions_for(角色)`」。

    ⚠️ 它**不是授权依据**：强制校验仍在 `require_permission`。
    """
    from app.modules.authorization.service import permissions_for

    user = await make_user("legal_reviewer")
    async with make_client() as client:
        login_response = await login(client, user.username)
        assert login_response.status_code == 200
        body = login_response.json()
        assert sorted(body["permissions"]) == sorted(permissions_for({"legal_reviewer"}))
        assert "review.decide" in body["permissions"]

        me_response = await client.get("/api/v1/auth/me")
        assert me_response.status_code == 200
        assert sorted(me_response.json()["permissions"]) == sorted(body["permissions"])


async def test_unknown_role_grants_nothing():
    """未知角色不授予任何权限——`permissions_for` 是 `can()` 的唯一实现。"""
    from app.modules.authorization.service import permissions_for

    assert permissions_for({"nobody"}) == frozenset()
    assert permissions_for(set()) == frozenset()
