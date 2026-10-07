"""§9.3 第二层语义校验（`app/modules/answering/semantics.py`）。

**不连数据库、不加载模型**：判官本身由 `generate` 注入，这里喂的是**假的判官输出**，
钉住的是「判官说了什么之后，服务端怎么处理」——尤其是那两道**确定性核对**（原句必须真的在条文里、
「可以/应当」不能被互换）。判官本人的水平由 `scripts/evaluate_semantics.py` 在金标准上量。
"""

from dataclasses import dataclass

from app.modules.answering import semantics
from app.modules.answering.claims import Claim


@dataclass(frozen=True)
class _Item:
    instrument_title: str
    provision_number: str
    provision_display: str
    legal_status: str
    text: str


EVIDENCE = [
    _Item(
        "中华人民共和国劳动法",
        "100",
        "第一百条",
        "unknown",
        "第一百条　用人单位无故不缴纳社会保险费的，由劳动行政部门责令其限期缴纳；"
        "逾期不缴的，可以加收滞纳金。",
    ),
    _Item(
        "中华人民共和国注册会计师法",
        "45",
        "第四十五条",
        "unknown",
        "第四十五条　除国家另有规定外，任何单位或者个人不得擅自携带、传递审计工作底稿出境。",
    ),
]


def _judge(payload: str):
    """把判官换成一个固定输出。`generate` 的签名是 `(messages, schema)`。"""

    def generate(_messages, schema=None):
        return payload

    return generate


def test_parses_a_clean_verdict():
    review = semantics.parse_review(
        '{"supported": true, "evidence_quote": "工资应当以货币形式支付", "issues": []}'
    )
    assert review is not None and review.supported is True


def test_unparseable_verdict_is_none_not_guessed():
    """判官给不出可解析的结论时**不能猜**——由调用方按「核验不可用」处理（§9.4 不跳过核验）。"""
    assert semantics.parse_review("这条主张是对的。") is None
    assert semantics.parse_review('{"issues": []}') is None
    assert semantics.parse_review("") is None


def test_supported_verdict_requires_a_quote_from_the_provision():
    """判官说支持，就得交出条文里的原句——**这是给语义判断加的可核对锚点**。"""
    claim = Claim(text="逾期不缴的可以加收滞纳金", evidence_ids=["1"])
    good = semantics.review_claim(
        claim,
        "会怎样？",
        EVIDENCE,
        _judge(
            '{"supported": true, "evidence_quote": "逾期不缴的，可以加收滞纳金。", "issues": []}'
        ),
    )
    assert good.supported is True

    bad = semantics.review_claim(
        claim,
        "会怎样？",
        EVIDENCE,
        _judge('{"supported": true, "evidence_quote": "逾期不缴的应当加收滞纳金。", "issues": []}'),
    )
    assert bad.supported is False
    assert bad.issues[0].kind == semantics.UNVERIFIABLE_QUOTE


def test_quote_may_carry_the_citation_prefix():
    """实测判官会把出处一起抄进原句（`《某法》第X条 …`），而条文正文里没有法律名称那一段。"""
    assert semantics.quote_in_evidence(
        "《中华人民共和国劳动法》第一百条 用人单位无故不缴纳社会保险费的", EVIDENCE
    )
    assert semantics.quote_in_evidence("用人单位无故不缴纳社会保险费的", EVIDENCE)


def test_short_or_absent_quotes_do_not_count():
    """太短的片段到处都有，命中了也证明不了什么。"""
    assert semantics.quote_in_evidence("可以加收", EVIDENCE) is False
    assert semantics.quote_in_evidence("工资应当以货币形式按月支付", EVIDENCE) is False


def test_modal_swap_is_caught_deterministically():
    """「可以」被说成「应当」是实质性错误——判官抄对了原句却看不出，所以用确定性规则补上。"""
    assert semantics.modal_mismatch("逾期不缴的应当加收滞纳金", "逾期不缴的，可以加收滞纳金。")
    assert semantics.modal_mismatch("工资可以实物发放", "工资应当以货币形式按月支付。")
    # 两边一致、或引文里本来就两种都有，都不算错
    assert (
        semantics.modal_mismatch("逾期不缴的可以加收滞纳金", "逾期不缴的，可以加收滞纳金。") is None
    )
    assert (
        semantics.modal_mismatch("应当依法支付工资", "用人单位应当依法支付工资，也可以提前支付。")
        is None
    )


def test_modal_swap_downgrades_a_supported_verdict():
    claim = Claim(text="用人单位逾期不缴纳社会保险费的，应当加收滞纳金", evidence_ids=["1"])
    verdict = semantics.review_claim(
        claim,
        "会怎样？",
        EVIDENCE,
        _judge(
            '{"supported": true, "evidence_quote": "逾期不缴的，可以加收滞纳金。", "issues": []}'
        ),
    )
    assert verdict.supported is False
    assert "应当" in verdict.summary() and "可以" in verdict.summary()


def test_unsupported_verdict_passes_through_untouched():
    claim = Claim(text="工资可以用实物发放", evidence_ids=["1"])
    verdict = semantics.review_claim(
        claim,
        "工资怎么发？",
        EVIDENCE,
        _judge(
            '{"supported": false, "evidence_quote": "",'
            ' "issues": [{"kind": "contradicts", "detail": "条文写的是应当以货币形式支付"}]}'
        ),
    )
    assert verdict.supported is False
    assert verdict.issues[0].kind == "contradicts"


def test_review_fails_the_whole_answer_when_any_claim_fails():
    """§9.4「正式答案通过门禁后发送」——部分通过也还是没通过。"""
    from app.modules.answering.claims import StructuredAnswer

    answer = StructuredAnswer.model_validate(
        {
            "claims": [
                {"text": "逾期不缴的可以加收滞纳金", "evidence_ids": ["1"]},
                {"text": "工资可以用实物发放", "evidence_ids": ["1"]},
            ]
        }
    )
    verdict = semantics.review(
        answer,
        "问题",
        EVIDENCE,
        _judge(
            '{"supported": false, "evidence_quote": "", "issues": [{"kind": "contradicts", "detail": "反了"}]}'
        ),
    )
    assert verdict.ok is False
    assert len(verdict.reviews) == 2


def test_review_marks_unavailable_instead_of_failing_silently():
    """§9.4「语义核验模型不可用时**不跳过核验**发布完整结论」——按不通过处理，但要标出来。"""
    from app.modules.answering.claims import StructuredAnswer

    answer = StructuredAnswer.model_validate({"claims": [{"text": "结论", "evidence_ids": ["1"]}]})
    verdict = semantics.review(answer, "问题", EVIDENCE, _judge("判官没按格式说话"))
    assert verdict.ok is False
    assert verdict.unavailable is True
    assert verdict.reason


def test_empty_claim_list_passes_vacuously():
    """拒答（没有主张）没有可核验的东西——不该因此被拦。"""
    from app.modules.answering.claims import StructuredAnswer

    answer = StructuredAnswer.model_validate({"claims": []})
    verdict = semantics.review(answer, "问题", EVIDENCE, _judge("{}"))
    assert verdict.ok is True
    assert verdict.reviews == ()
