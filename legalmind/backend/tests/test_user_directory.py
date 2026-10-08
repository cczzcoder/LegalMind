"""可授权对象名单（`GET /api/v1/users/directory`）。

**为什么单独钉**：`GET /users` 要 `user.manage`（只有 `system_admin` 有），而
`document.grant` / `wiki.grant` 在 `knowledge_admin` 手里——**有授权权的人列不出用户，
就只能靠粘贴 UUID 发授权**，那等于把「授权」这件事挡在了门外。这里钉三件事：

1. **授权人拿得到**（不必是用户管理员），**普通读者拿不到**；
2. **范围限本组织**（§21 按租户隔离）；
3. **刻意的窄**：只有 id / 用户名 / 是否启用，不含角色与 MFA 状态。
"""

import pytest

from tests.helpers import login

pytestmark = pytest.mark.anyio


async def test_a_grantor_can_list_the_directory(make_client, make_user):
    admin = await make_user("knowledge_admin")
    mate = await make_user("reader", organization_id=admin.organization_id)

    async with make_client() as client:
        await login(client, admin.username)
        response = await client.get("/api/v1/users/directory")

    assert response.status_code == 200, response.text
    ids = {row["id"] for row in response.json()}
    assert {str(admin.id), str(mate.id)} <= ids


async def test_the_directory_is_scoped_to_my_organization(make_client, make_user):
    admin = await make_user("knowledge_admin")
    outsider = await make_user("reader")  # make_user 每次都建新组织

    async with make_client() as client:
        await login(client, admin.username)
        response = await client.get("/api/v1/users/directory")

    assert str(outsider.id) not in {row["id"] for row in response.json()}, (
        "名单限本组织——跨组织的用户名不该在这里露出来（§21）"
    )


async def test_the_directory_is_deliberately_narrow(make_client, make_user):
    """只有 id / 用户名 / 是否启用。**角色与 MFA 状态不给**——授权人要回答的是
    「把这条资料的访问权给谁」，不是「组织里都有谁、各自什么权限」。"""
    admin = await make_user("knowledge_admin")

    async with make_client() as client:
        await login(client, admin.username)
        response = await client.get("/api/v1/users/directory")

    row = next(item for item in response.json() if item["id"] == str(admin.id))
    assert set(row) == {"id", "username", "is_active"}
    assert row["is_active"] is True


async def test_a_plain_reader_is_refused(make_client, make_user):
    reader = await make_user("reader")
    async with make_client() as client:
        await login(client, reader.username)
        response = await client.get("/api/v1/users/directory")
    assert response.status_code == 403
