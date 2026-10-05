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
