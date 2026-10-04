"""入库质量门禁的取数（设计 §7、§17.1）：需要 TEST_DATABASE_URL。

走一遍真实的「导入 → 解析 → 挂版本」链路，断言 ``collect_facts`` 能把门禁事实从库里还原，
且高置信度落库判 passed、低置信度落库判 degraded——即流水线的「自动落库 / 降级待审」确实生效。
"""

from uuid import uuid4

import pytest

from app.adapters.storage import LocalFileStorage, get_storage
from app.core.security import Principal
from app.modules.legal_corpus.quality import (
    DEGRADED,
    PASSED,
    REASON_REVIEW_PENDING,
    assess,
    collect_facts,
)
from app.modules.parsing.service import parse_artifact
from tests.helpers import import_document_for_parsing

pytestmark = pytest.mark.anyio


@pytest.fixture
def storage(tmp_path, make_client):
    from app.main import app

    storage = LocalFileStorage(tmp_path)
    app.dependency_overrides[get_storage] = lambda: storage
    return storage


def _principal(user) -> Principal:
    return Principal(organization_id=user.organization_id, user_id=user.id, roles=frozenset())


async def _facts_for(filename: str, session_factory) -> list:
    async with session_factory() as session:
        facts = await collect_facts(session)
    return [fact for fact in facts if fact.filename == filename]


async def test_high_confidence_law_passes_the_gate(
    make_client, make_user, storage, session_factory
):
    name = f"示例测试法（{uuid4().hex[:8]}）"
    filename = f"{name}.txt"
    content = (
        f"{name}\n（2026年5月1日第十四届全国人民代表大会常务委员会第一次会议通过）\n"
        "第一条　为了测试，制定本法。\n"
    ).encode()
    editor, document_id = await import_document_for_parsing(
        make_client, make_user, content, filename
    )
    async with session_factory() as session:
        await parse_artifact(session, storage, _principal(editor), document_id)

    mine = await _facts_for(filename, session_factory)
    assert len(mine) == 1
    fact = mine[0]
    assert fact.landed is True
    assert fact.chunk_count and fact.chunk_count > 0
    verdict = assess(fact)
    assert verdict.status == PASSED, verdict.reasons


async def test_weak_version_lands_as_degraded(make_client, make_user, storage, session_factory):
    """正文无公布信息、只能回退文件名日期：流水线置 pending，门禁判 degraded。"""
    name = f"示例测试条例（{uuid4().hex[:8]}）"
    filename = f"{name}_20260515.txt"
    content = f"{name}\n第一条　为了测试。\n".encode()
    editor, document_id = await import_document_for_parsing(
        make_client, make_user, content, filename
    )
    async with session_factory() as session:
        await parse_artifact(session, storage, _principal(editor), document_id)

    mine = await _facts_for(filename, session_factory)
    assert len(mine) == 1
    fact = mine[0]
    assert fact.landed is True
    assert fact.review_status == "pending"
    verdict = assess(fact)
    assert verdict.status == DEGRADED
    assert REASON_REVIEW_PENDING in verdict.reasons
