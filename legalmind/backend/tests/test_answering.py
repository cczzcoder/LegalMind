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
from app.modules.retrieval.schemas import CitationOut, ProvisionHit

pytestmark = pytest.mark.anyio


def _hit(title: str, number: str, text: str, display: str | None = None) -> ProvisionHit:
    return ProvisionHit(
        instrument_title=title,
        instrument_type="law",
        jurisdiction="CN",
        issuing_body="测试机关",
        document_number=None,
        version_label="2026年",
        legal_status="effective",
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

    name = f"示例测试法（{uuid4().hex[:8]}）"
    content = (
        f"{name}\n（2026年5月1日第十四届全国人民代表大会常务委员会第一次会议通过）\n"
        "第一条　用人单位无故不缴纳社会保险费的，由劳动行政部门责令其限期缴纳。\n"
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

    monkeypatch.setattr(generation, "generate", lambda *_a, **_k: "这是模型生成的答案（测试替身）")
    user = await make_user("reader")
    principal = Principal(
        organization_id=user.organization_id, user_id=user.id, roles=frozenset({"reader"})
    )

    # 查询写成空格分隔的多词，且每个词都确实出现在条文里：测试库没有向量，级联的兜底段是空的，
    # 得靠关键词段命中（`all_terms_flat` 要求每个词都命中，写错一个词整条就落空）
    async with session_factory() as session:
        answer = await service.answer_question(
            session, principal, "用人单位 社会保险费 劳动行政部门"
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
