"""统一错误响应与 trace_id 的单元测试（设计 13、16.1）。

不依赖数据库：未带会话 Cookie 访问业务接口时，鉴权依赖在查库前即返回 401。
"""

import httpx
import pytest

from app.main import app

pytestmark = pytest.mark.anyio


async def _get(path: str) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get(path)


async def test_error_body_has_code_message_and_trace_id():
    response = await _get("/api/v1/wiki/pages")

    assert response.status_code == 401
    body = response.json()
    assert body["code"] == "unauthorized"
    assert body["message"] == "Not authenticated"
    assert body["trace_id"]
    # trace_id 同时回写在响应头，便于用户报错时提供服务端可检索的标识
    assert response.headers["x-trace-id"] == body["trace_id"]


async def test_trace_id_is_unique_per_request():
    first = (await _get("/api/v1/wiki/pages")).json()["trace_id"]
    second = (await _get("/api/v1/wiki/pages")).json()["trace_id"]

    assert first != second


async def test_unknown_path_also_uses_unified_body():
    response = await _get("/api/v1/does-not-exist")

    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "not_found"
    assert body["trace_id"]
