"""证据约束问答（设计 §9；P6 的第一刀）。

**不加载生成模型**——本机加载 7B 量化权重很慢，测试里不该做。这里只钉住两件与模型无关、
但错了会直接伤害可信度的事：**没有依据时必须拒答且不调用模型**，以及**证据块只放条文原文**。
真正的生成质量要等评测（§9.5「本地生成模型单独评测，达标后再接入正式问答」）。
"""

from uuid import uuid4

import pytest
from sqlalchemy import select

from app.adapters import generation
from app.core.security import Principal
from app.models import LegalInstrument, LegalVersion, ProvisionIdentity, ProvisionVersion
from app.modules.answering import service
from app.modules.retrieval.schemas import CitationOut, ProvisionHit, SearchResponse

pytestmark = pytest.mark.anyio


def _hit(
    title: str,
    number: str,
    text: str,
    display: str | None = None,
    status: str = "effective",
) -> ProvisionHit:
    return ProvisionHit(
        instrument_title=title,
        instrument_type="law",
        jurisdiction="CN",
        issuing_body="测试机关",
        document_number=None,
        version_label="2026年",
        legal_status=status,
        review_status="approved",
        promulgated_on=None,
        effective_from=None,
        effective_to=None,
        provision_type="article",
        provision_number=number,
        provision_display=display,
        text=text,
        text_sha256="0" * 64,
        citation=CitationOut(
            instrument_id=uuid4(),
            legal_version_id=uuid4(),
            provision_version_id=uuid4(),
            provision_identity_id=uuid4(),
            artifact_id=uuid4(),
            chunk_id=uuid4(),
        ),
    )


def _pinned_search(hits, path: str = "keyword"):
    """把检索换成固定结果——效力状态门禁要测的是回答层，不是检索。"""

    async def search(_session, _principal, _query):
        return SearchResponse(hits=list(hits), truncated=False, path=path)

    return search


def test_evidence_block_carries_only_the_provision_text():
    """证据块里只放条文与出处——不放别的东西，模型的输入面越窄越可核验。"""
    evidence = service.build_evidence([_hit("中华人民共和国监狱法", "50", "第五十条　监狱提请……")])
    assert "中华人民共和国监狱法" in evidence
    assert "第五十条" in evidence
    # 效力状态**翻成中文**再给模型：塞英文枚举值 ``unknown`` 会让小模型读成「不可靠」而拒答
    assert "（现行有效）" in evidence
    assert "监狱提请" in evidence


def test_evidence_block_falls_back_to_the_number_without_a_display_title():
    evidence = service.build_evidence([_hit("中华人民共和国监狱法", "50", "正文", display=None)])
    assert "50" in evidence


async def test_answer_without_evidence_refuses_and_never_calls_the_model(
    monkeypatch, session_factory, make_user
):
    """§9.5：本地模型不满足质量标准时提供证据检索与人工审核，**不开放正式自动结论**。

    检索一条都没命中就不该生成——让模型对着空证据说话，正是最危险的那种"看起来有答案"。
    """
    called = False

    def explode(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("没有依据时不应调用生成模型")

    monkeypatch.setattr(generation, "generate", explode)
    user = await make_user("reader")
    principal = Principal(
        organization_id=user.organization_id, user_id=user.id, roles=frozenset({"reader"})
    )

    async with session_factory() as session:
        answer = await service.answer_question(
            session, principal, "量子计算专利强制许可的审查标准是什么？"
        )

    assert called is False
    assert answer.answer == service.NO_EVIDENCE
    assert answer.citations == ()
    assert answer.model is None


async def test_evidence_that_is_not_current_refuses_to_conclude(
    monkeypatch, session_factory, make_user
):
    """§8.3 + §9.5：拿一部尚未生效的法律去下结论，正是最该防的那件事。

    **这条不能交给模型**：实测（§9.5 的生成质量评测）证据块里明写「（尚未生效，不得作为现行
    依据）」，模型照样把它当现行依据陈述（`status_flag_rate` 为 0）。所以由回答层确定性拒答，
    且**不调用模型**——生成结论本来就没有意义。
    """
    called = False

    def explode(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("依据全部不是现行有效时不应调用生成模型")

    monkeypatch.setattr(generation, "generate", explode)
    monkeypatch.setattr(
        service,
        "search_provisions",
        _pinned_search([_hit("中华人民共和国某法", "2", "第二条　……", status="not_yet_effective")]),
    )
    user = await make_user("reader")
    principal = Principal(
        organization_id=user.organization_id, user_id=user.id, roles=frozenset({"reader"})
    )

    async with session_factory() as session:
        answer = await service.answer_question(session, principal, "某法怎么规定的？")

    assert called is False
    assert answer.model is None
    assert answer.status_notice is not None
    assert "现行有效" in answer.status_notice
    assert answer.answer == answer.status_notice
    assert len(answer.citations) == 1


async def test_status_notice_is_attached_when_only_some_evidence_is_not_current(
    monkeypatch, session_factory, make_user
):
    """只要有一条依据不能当现行依据，答案就带上提示——但结论照出（有效的那条还在）。"""
    monkeypatch.setattr(generation, "available", lambda *_a, **_k: True)
    monkeypatch.setattr(generation, "generate", lambda *_a, **_k: "模型给的结论")
    monkeypatch.setattr(
        service,
        "search_provisions",
        _pinned_search(
            [
                _hit("中华人民共和国甲法", "1", "第一条　……", status="effective"),
                _hit("中华人民共和国乙法", "9", "第九条　……", status="repealed"),
            ]
        ),
    )
    user = await make_user("reader")
    principal = Principal(
        organization_id=user.organization_id, user_id=user.id, roles=frozenset({"reader"})
    )

    async with session_factory() as session:
        answer = await service.answer_question(session, principal, "问题")

    assert answer.answer == "模型给的结论"
    assert answer.status_notice is not None
    assert "已被取代" in answer.status_notice


def test_unknown_status_is_not_treated_as_non_current():
    """`unknown` 只表示没提取到施行日期，**不等于失效**（提示词里也是这么告诉模型的）。

    把它算成不可用会把这份语料里大量正常条文判成不能用——13 部法律里有 7 部的状态就是 `unknown`。
    """
    citations = (
        service.Citation("甲法", "1", "第一条", "unknown", uuid4()),
        service.Citation("乙法", "2", "第二条", "effective", uuid4()),
    )
    assert service.status_notice_for(citations) is None
    assert service.non_current_citations(citations) == ()


def test_status_notice_lists_every_non_current_provision():
    citations = (
        service.Citation("甲法", "1", "第一条", "repealed", uuid4()),
        service.Citation("乙法", "2", "第二条", "not_yet_effective", uuid4()),
    )
    notice = service.status_notice_for(citations)
    assert notice is not None
    assert "甲法第一条" in notice and "已被取代" in notice
    assert "乙法第二条" in notice and "尚未生效" in notice
    assert service.status_notice_for(()) is None


async def test_oversized_evidence_refuses_instead_of_truncating(
    monkeypatch, session_factory, make_user
):
    """§9.4：法律原文不得因上下文限制被**无提示**截断——装不下就不生成结论。

    不设这条门禁的话，超长证据会被推理引擎**从前面静默截断**，而指令就在提示最前面——
    模型会先失去全部约束，再失去最早的条文。
    """
    called = False

    def explode(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("依据装不下时不应调用生成模型")

    monkeypatch.setattr(generation, "available", lambda *_a, **_k: True)
    monkeypatch.setattr(generation, "generate", explode)
    monkeypatch.setattr(
        service,
        "search_provisions",
        _pinned_search([_hit("中华人民共和国某法", "1", "文" * 20000)]),
    )
    user = await make_user("reader")
    principal = Principal(
        organization_id=user.organization_id, user_id=user.id, roles=frozenset({"reader"})
    )

    async with session_factory() as session:
        answer = await service.answer_question(session, principal, "某法怎么规定的？")

    assert called is False
    assert answer.model is None
    assert "上下文预算" in answer.answer
    assert answer.evidence_notice is not None


async def test_partial_assembly_answers_but_limits_the_scope(
    monkeypatch, session_factory, make_user
):
    """装下一部分时照出结论，但**明确限定范围**并点名没装进去的是哪几条（§9.4）。"""
    monkeypatch.setattr(generation, "available", lambda *_a, **_k: True)
    monkeypatch.setattr(generation, "generate", lambda *_a, **_k: "模型给的结论")
    monkeypatch.setattr(
        service,
        "search_provisions",
        _pinned_search(
            [
                _hit("中华人民共和国甲法", "1", "第一条　短条文。", display="第一条"),
                _hit("中华人民共和国乙法", "9", "第九条　" + "文" * 20000, display="第九条"),
            ]
        ),
    )
    user = await make_user("reader")
    principal = Principal(
        organization_id=user.organization_id, user_id=user.id, roles=frozenset({"reader"})
    )

    async with session_factory() as session:
        answer = await service.answer_question(session, principal, "问题")

    assert answer.answer == "模型给的结论"
    assert answer.evidence_notice is not None
    assert "乙法第九条" in answer.evidence_notice
    # 引用仍回传检索命中的全部条款（那是授权证据集），范围限制由 evidence_notice 说明
    assert len(answer.citations) == 2


@pytest.fixture
def storage(tmp_path, make_client):
    """存储依赖要在夹具里覆盖——在测试体内直接改 `dependency_overrides` 会泄漏到别的用例。"""
    from app.adapters.storage import LocalFileStorage, get_storage
    from app.main import app

    storage = LocalFileStorage(tmp_path)
    app.dependency_overrides[get_storage] = lambda: storage
    return storage


async def test_answer_returns_the_retrieved_provisions_as_citations(
    monkeypatch, make_client, make_user, session_factory, storage
):
    """引用来自**检索结果**而不是模型写的条号，所以天然可核验。

    这里把生成换掉——要验的是「引用等于检索命中」，不是模型写得好不好。
    """
    from app.modules.parsing.service import parse_artifact
    from tests.helpers import import_document_for_parsing

    # ⚠️ **必须让这条用例的条文里有一个全库唯一的词**：测试库跨运行累积，同一段条文已经被种了
    # 几百遍（实测 `示例测试法` 有 536 部），`limit=5` 的结果里根本轮不到刚建的这部。原先靠
    # 「标题带唯一后缀」不够——标题不在条款正文里，关键词段搜的是正文。
    token = uuid4().hex[:8]
    name = f"示例测试法（{token}）"
    content = (
        f"{name}\n（2026年5月1日第十四届全国人民代表大会常务委员会第一次会议通过）\n"
        f"第一条　{token}用人单位无故不缴纳社会保险费的，由劳动行政部门责令其限期缴纳。\n"
    ).encode()
    editor, document_id = await import_document_for_parsing(
        make_client, make_user, content, f"{name}.txt"
    )
    async with session_factory() as session:
        await parse_artifact(
            session,
            storage,
            Principal(organization_id=editor.organization_id, user_id=editor.id, roles=frozenset()),
            document_id,
        )

    monkeypatch.setattr(generation, "available", lambda *_a, **_k: True)
    monkeypatch.setattr(generation, "generate", lambda *_a, **_k: "这是模型生成的答案（测试替身）")
    user = await make_user("reader")
    principal = Principal(
        organization_id=user.organization_id, user_id=user.id, roles=frozenset({"reader"})
    )

    # 查询写成空格分隔的多词，且每个词都确实出现在条文里：测试库没有向量，级联的兜底段是空的，
    # 得靠关键词段命中（`all_terms_flat` 要求每个词都命中，写错一个词整条就落空）。
    # 带上唯一词，保证命中的就是刚建的这部法，而不是累积下来的同名旧数据。
    async with session_factory() as session:
        answer = await service.answer_question(
            session, principal, f"{token} 用人单位 社会保险费 劳动行政部门"
        )

    assert answer.answer == "这是模型生成的答案（测试替身）"
    assert answer.model is not None
    assert answer.citations, "检索有命中时引用不该为空"
    # 测试库共享且跨运行累积，别的用例也种了含相同词的法律——只要求**我这部法在引用里**
    assert name in {citation.instrument_title for citation in answer.citations}

    # 引用里的条款版本必须真的存在于库里——引用可核验的前提
    async with session_factory() as session:
        for citation in answer.citations:
            found = await session.scalar(
                select(ProvisionVersion.id)
                .join(
                    ProvisionIdentity,
                    ProvisionVersion.provision_identity_id == ProvisionIdentity.id,
                )
                .join(LegalVersion, ProvisionVersion.legal_version_id == LegalVersion.id)
                .join(LegalInstrument, LegalVersion.instrument_id == LegalInstrument.id)
                .where(
                    ProvisionVersion.id == citation.provision_version_id,
                    LegalInstrument.title == citation.instrument_title,
                )
            )
            assert found is not None


@pytest.mark.parametrize("module", ["app.modules.answering.service"])
def test_answering_has_no_external_api_path(module):
    """§9.5 的决策：P6 架构锁定本地模型、**默认关闭任何外部 API 接口**。

    用「源码里不出现外部推理服务的调用与配置」来守这条线——不靠评审记得。
    """
    import importlib
    import inspect

    source = inspect.getsource(importlib.import_module(module)).lower()
    for forbidden in ("openai", "anthropic", "dashscope", "api_key", "https://api."):
        assert forbidden not in source, f"{module} 出现了外部推理服务的痕迹：{forbidden}"
