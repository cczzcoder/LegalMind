"""精确字段检索集成测试（设计 §8.2 的精确通路、§8.3）：需要 TEST_DATABASE_URL。

覆盖：按条号/名称/文号精确匹配并回传引用锚点与原文定位、默认屏蔽「已公布未生效」、
``effective_on`` 只排除能证明不在效期内的版本、受限原件需授权、条号解析失败返回空而不是放宽条件。
"""

from uuid import uuid4

import pytest

from app.adapters.storage import LocalFileStorage, get_storage
from app.core.security import Principal
from app.modules.parsing.service import parse_artifact
from app.modules.retrieval.schemas import SearchQuery
from app.modules.retrieval.service import search_provisions
from tests.helpers import (
    import_document_for_parsing,
    import_document_into_source,
    login,
    setup_source,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def storage(tmp_path, make_client):
    from app.main import app

    storage = LocalFileStorage(tmp_path)
    app.dependency_overrides[get_storage] = lambda: storage
    return storage


def _principal(user) -> Principal:
    return Principal(organization_id=user.organization_id, user_id=user.id, roles=frozenset())


def law_name() -> str:
    return f"示例测试法（{uuid4().hex[:8]}）"


def law_text(
    name: str,
    *,
    passed: str = "2026年5月1日",
    effective: str = "",
    phone: str | None = None,
    order_number: str | None = None,
) -> bytes:
    """构造一份最小法律文本；``order_number`` 会作为主席令文号写进前言。"""
    order = f"\n中华人民共和国主席令\n{order_number}" if order_number else ""
    clause = f"　{effective}" if effective else ""
    body = "第一条　为了测试，制定本法。"
    if phone:
        body += f"\n第二条　联系方式为{phone}，请依法处理。"
    return (
        f"{order}\n{name}\n（{passed}第十四届全国人民代表大会常务委员会第一次会议通过{clause}）\n"
        f"{body}\n"
    ).encode()


async def _seed(make_client, make_user, storage, session_factory, content, filename):
    editor, document_id = await import_document_for_parsing(
        make_client, make_user, content, filename
    )
    async with session_factory() as session:
        await parse_artifact(session, storage, _principal(editor), document_id)
    return editor, document_id


async def test_search_by_article_number_returns_provision_with_citation(
    make_client, make_user, storage, session_factory
):
    name = law_name()
    editor, document_id = await _seed(
        make_client,
        make_user,
        storage,
        session_factory,
        law_text(name, effective="自2026年6月1日起施行", phone="13800138000"),
        f"{name}.txt",
    )

    async with session_factory() as session:
        response = await search_provisions(
            session, _principal(editor), SearchQuery(instrument_title=name, article_number="第一条")
        )

    assert len(response.hits) == 1
    hit = response.hits[0]
    assert hit.provision_number == "1"
    assert hit.provision_display == "第一条"
    assert hit.text.startswith("第一条")
    assert hit.instrument_title == name
    assert hit.legal_status == "effective"
    assert hit.review_status == "approved"
    assert hit.effective_from.isoformat() == "2026-06-01"
    # 引用锚点必须足以定位到具体版本与原文位置（设计 §5.2、§8.3）
    assert hit.citation.artifact_id == document_id
    assert hit.citation.chunk_id is not None
    assert hit.citation.char_start is not None
    assert hit.citation.char_end > hit.citation.char_start
    # 纯文本没有页概念：定位只有字符偏移，页码存 NULL 而不是编一个 0（设计 §6）
    assert hit.citation.page_index is None
    assert hit.citation.printed_page_label is None


async def test_search_matches_the_document_number_in_both_writings(
    make_client, make_user, storage, session_factory
):
    """文号按规范化值精确匹配：「…主席令第七十七号」与「…主席令第77号」都能命中。

    带 ``instrument_title`` 是为了把断言隔离到本次用例的法律上——测试库跨运行累积，
    固定文号会命中历次运行留下的同名记录。
    """
    name = law_name()
    editor, _ = await _seed(
        make_client,
        make_user,
        storage,
        session_factory,
        law_text(name, order_number="第七十七号"),
        f"{name}.txt",
    )

    async with session_factory() as session:
        for written in ("中华人民共和国主席令第七十七号", "中华人民共和国主席令第77号"):
            response = await search_provisions(
                session,
                _principal(editor),
                SearchQuery(instrument_title=name, document_number=written),
            )
            assert len(response.hits) == 1, written
            assert response.hits[0].instrument_title == name
            # 展示值仍是原样，不被规范化改写
            assert response.hits[0].document_number == "中华人民共和国主席令第七十七号"

        wrong = await search_provisions(
            session,
            _principal(editor),
            SearchQuery(instrument_title=name, document_number="中华人民共和国主席令第七十八号"),
        )
    assert wrong.hits == []


async def test_not_yet_effective_is_hidden_unless_asked_for(
    make_client, make_user, storage, session_factory
):
    """默认屏蔽「已公布未生效」；显式放开才返回（设计 §8.3）。"""
    name = law_name()
    editor, _ = await _seed(
        make_client,
        make_user,
        storage,
        session_factory,
        law_text(name, effective="自2027年1月1日起施行"),
        f"{name}.txt",
    )

    async with session_factory() as session:
        hidden = await search_provisions(
            session, _principal(editor), SearchQuery(instrument_title=name)
        )
        shown = await search_provisions(
            session,
            _principal(editor),
            SearchQuery(instrument_title=name, include_not_yet_effective=True),
        )

    assert hidden.hits == []
    assert len(shown.hits) == 1
    assert shown.hits[0].legal_status == "not_yet_effective"


async def test_effective_on_only_excludes_versions_proven_out_of_force(
    make_client, make_user, storage, session_factory
):
    """``effective_on`` 只排除能证明不在效期内的版本；施行日期未知的版本保留。"""
    dated = law_name()
    undated = law_name()
    editor, _ = await _seed(
        make_client,
        make_user,
        storage,
        session_factory,
        law_text(dated, effective="自2026年6月1日起施行"),
        f"{dated}.txt",
    )
    await _seed(
        make_client,
        make_user,
        storage,
        session_factory,
        law_text(undated),
        f"{undated}.txt",
    )

    async with session_factory() as session:
        after = await search_provisions(
            session,
            _principal(editor),
            SearchQuery(instrument_title=dated, effective_on="2026-06-15"),
        )
        before = await search_provisions(
            session,
            _principal(editor),
            SearchQuery(instrument_title=dated, effective_on="2026-05-15"),
        )
        unknown = await search_provisions(
            session,
            _principal(editor),
            SearchQuery(instrument_title=undated, effective_on="2026-05-15"),
        )

    assert len(after.hits) == 1
    # 施行日期晚于该日：能证明当时还没生效，排除
    assert before.hits == []
    # 施行日期未知：不当作"当时无效"，保留并在结果里如实显示 unknown
    assert len(unknown.hits) == 1
    assert unknown.hits[0].legal_status == "unknown"


async def test_unparsable_article_number_returns_nothing(
    make_client, make_user, storage, session_factory
):
    """条号解析不出来时返回空结果，而不是静默放宽条件返回全部条款。"""
    name = law_name()
    editor, _ = await _seed(
        make_client,
        make_user,
        storage,
        session_factory,
        law_text(name, phone="13800138000"),
        f"{name}.txt",
    )

    async with session_factory() as session:
        response = await search_provisions(
            session,
            _principal(editor),
            SearchQuery(instrument_title=name, article_number="第九十九条"),
        )
        garbage = await search_provisions(
            session, _principal(editor), SearchQuery(instrument_title=name, article_number="abc")
        )

    # 该法律没有第九十九条
    assert response.hits == []
    assert garbage.hits == []


async def test_restricted_document_is_only_visible_with_a_grant(
    make_client, make_user, storage, session_factory
):
    """授权在数据库里复核（设计 §11.2、§8.3）：受限原件没授权就不出现在结果里。"""
    name = law_name()
    admin, editor, source_id = await setup_source(make_client, make_user)
    outsider = await make_user("reader", organization_id=admin.organization_id)
    document_id = await import_document_into_source(
        make_client,
        editor,
        source_id,
        law_text(name),
        f"{name}.txt",
        access_scope="restricted",
    )
    async with session_factory() as session:
        await parse_artifact(session, storage, _principal(editor), document_id)

    async with session_factory() as session:
        hidden = await search_provisions(
            session, _principal(outsider), SearchQuery(instrument_title=name)
        )
        visible = await search_provisions(
            session, _principal(editor), SearchQuery(instrument_title=name)
        )

    assert hidden.hits == []
    assert len(visible.hits) == 1

    # 授权后即可见（knowledge_admin 管理授权）
    async with make_client() as client:
        await login(client, admin.username)
        granted = await client.put(f"/api/v1/documents/{document_id}/grants/{outsider.id}")
    assert granted.status_code == 200

    async with session_factory() as session:
        after = await search_provisions(
            session, _principal(outsider), SearchQuery(instrument_title=name)
        )
    assert len(after.hits) == 1


async def test_limit_truncates_and_reports_it(make_client, make_user, storage, session_factory):
    name = law_name()
    editor, _ = await _seed(
        make_client,
        make_user,
        storage,
        session_factory,
        law_text(name, phone="13800138000"),
        f"{name}.txt",
    )

    async with session_factory() as session:
        limited = await search_provisions(
            session, _principal(editor), SearchQuery(instrument_title=name, limit=1)
        )
        full = await search_provisions(
            session, _principal(editor), SearchQuery(instrument_title=name)
        )

    assert len(limited.hits) == 1
    assert limited.truncated is True
    assert len(full.hits) == 2
    assert full.truncated is False


async def test_instrument_type_filter(make_client, make_user, storage, session_factory):
    name = law_name()
    editor, _ = await _seed(
        make_client, make_user, storage, session_factory, law_text(name), f"{name}.txt"
    )

    async with session_factory() as session:
        matching = await search_provisions(
            session,
            _principal(editor),
            SearchQuery(instrument_types=["law"], instrument_title=name),
        )
        mismatching = await search_provisions(
            session,
            _principal(editor),
            SearchQuery(instrument_types=["constitution"], instrument_title=name),
        )

    assert len(matching.hits) == 1
    assert mismatching.hits == []


async def test_search_endpoint_requires_document_read(
    make_client, make_user, storage, session_factory
):
    """走 HTTP 端点：reader 能检索，没有任何业务权限的 auditor 被拒。"""
    name = law_name()
    reader = await make_user("reader")
    auditor = await make_user("auditor")
    await _seed(make_client, make_user, storage, session_factory, law_text(name), f"{name}.txt")

    async with make_client() as client:
        await login(client, reader.username)
        response = await client.post(
            "/api/v1/search", json={"instrument_title": name, "article_number": "第一条"}
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["truncated"] is False
    assert len(body["hits"]) == 1
    hit = body["hits"][0]
    assert hit["instrument_title"] == name
    assert hit["provision_number"] == "1"
    # 结果必须携带效力状态与审核状态（设计 §8.3）
    assert hit["legal_status"] in ("effective", "not_yet_effective", "repealed", "unknown")
    assert hit["review_status"] in ("pending", "approved", "rejected")
    assert set(hit["citation"]) >= {
        "instrument_id",
        "legal_version_id",
        "provision_version_id",
        "artifact_id",
    }

    async with make_client() as client:
        await login(client, auditor.username)
        denied = await client.post("/api/v1/search", json={"instrument_title": name})

    assert denied.status_code == 403


async def test_search_rejects_unknown_filter_values(
    make_client, make_user, storage, session_factory
):
    """过滤取值写错时报 422，而不是静默返回空结果让人以为"查无此法"。"""
    reader = await make_user("reader")
    async with make_client() as client:
        await login(client, reader.username)
        bad_type = await client.post("/api/v1/search", json={"instrument_types": ["statute"]})
        bad_status = await client.post("/api/v1/search", json={"review_statuses": ["ok"]})
        bad_limit = await client.post("/api/v1/search", json={"limit": 0})

    assert bad_type.status_code == 422
    assert bad_status.status_code == 422
    assert bad_limit.status_code == 422
