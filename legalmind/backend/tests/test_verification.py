"""§9.3 第一层的确定性核验（`app/modules/answering/verification.py`）。

**不连数据库、不加载模型**——核验本来就是纯函数，所以能这样钉住。
真实效果由 `scripts/evaluate_answering.py` 在 22 条金标准上报告（当前触发率 0）。
"""

from dataclasses import dataclass

from app.modules.answering import verification


@dataclass(frozen=True)
class _Item:
    instrument_title: str
    provision_number: str
    provision_display: str
    legal_status: str
    text: str


def _item(law: str, article: str, text: str, status: str = "effective", display: str | None = None):
    return _Item(law, article, display or f"第{article}条", status, text)


EVIDENCE = [
    _item(
        "中华人民共和国劳动法",
        "50",
        "第五十条　工资应当以货币形式按月支付给劳动者本人。不得克扣或者无故拖欠劳动者的工资。",
    ),
    _item(
        "中华人民共和国劳动法",
        "100",
        "第一百条　用人单位无故不缴纳社会保险费的，由劳动行政部门责令其限期缴纳；"
        "逾期不缴的，可以加收滞纳金。",
    ),
]


def _kinds(answer: str, evidence=None, question: str = "工资怎么发？"):
    result = verification.verify(answer, question, evidence if evidence is not None else EVIDENCE)
    return [issue.kind for issue in result.issues]


def test_grounded_answer_passes():
    answer = (
        "不可以。依据《中华人民共和国劳动法》第五十条，工资应当以货币形式按月支付给劳动者本人。"
    )
    result = verification.verify(answer, "工资怎么发？", EVIDENCE)
    assert result.ok is True
    assert result.issues == ()


def test_article_number_outside_the_evidence_is_rejected():
    """把依据说成不存在的那一条，是最该拦下的错误。"""
    assert _kinds("依据《中华人民共和国劳动法》第一百零一条，工资可以实物发放。") == [
        "unwarranted_article"
    ]


def test_cross_reference_inside_the_evidence_text_is_allowed():
    """条文自己写着「依照本法第五十一条规定」——模型复述它不算编造。"""
    evidence = [
        _item(
            "中华人民共和国监狱法",
            "50",
            "第五十条　监狱提出减刑、假释建议，应当经监狱减刑假释评审委员会评审。"
            "依照本法第五十一条规定需要报经省、自治区、直辖市监狱管理机关审核同意的……",
        )
    ]
    answer = "依据《中华人民共和国监狱法》第五十条，评审后还要依照本法第五十一条报审核。"
    assert verification.verify(answer, "减刑要什么程序？", evidence).ok is True


def test_naming_a_law_without_citing_an_article_is_not_a_citation():
    """实测 22 条里唯一一条误报就是这种：模型在**说明缺什么**，不是在拿那条法当依据。"""
    answer = "依据不足。因为提供的条文为空白，没有关于机动车交通事故责任的具体内容。（《中华人民共和国道路交通安全法》未提供相关条款）"
    assert verification.verify(answer, "交通事故责任怎么划分？", []).ok is True


def test_law_short_title_is_matched_exactly_not_by_prefix():
    """「药品管理法」不能被「药品管理法实施条例」掩盖——`legal_corpus/citations.py` 踩过这个坑。"""
    evidence = [
        _item(
            "中华人民共和国药品管理法实施条例",
            "45",
            "第四十五条　药品网络交易第三方平台提供者应当建立健全药品网络销售质量管理体系。",
        )
    ]
    answer = "依据《中华人民共和国药品管理法》第十五条，可以走加快通道。"
    assert "unwarranted_law" in _kinds(answer, evidence)
    assert "unwarranted_article" in _kinds(answer, evidence)


def test_numbers_must_come_from_the_evidence_or_the_question():
    evidence = [
        _item(
            "中华人民共和国矿产资源法实施条例",
            "15",
            "第十五条　探矿权的期限为5年，期限届满可以续期，续期最多不超过3次。",
        )
    ]
    assert verification.verify("依据该条例第十五条，探矿权期限为5年。", "探矿权多久？", evidence).ok
    assert _kinds("依据该条例第十五条，探矿权期限为20年。", evidence) == ["unsupported_number"]


def test_list_markers_and_article_digits_are_not_treated_as_numbers():
    """「1.」「（2）」是序号，「第100条」是条号——都不该被当成"数值"来查。"""
    evidence = [_item("中华人民共和国劳动法", "100", "第一百条　用人单位无故不缴纳社会保险费的……")]
    answer = (
        "1. 依据《中华人民共和国劳动法》第100条：责令其限期缴纳。\n2. 逾期不缴的可以加收滞纳金。"
    )
    assert verification.verify(answer, "欠缴社保费怎么处理？", evidence).ok is True


def test_quotes_must_appear_in_the_evidence_text():
    assert verification.verify(
        "依据《中华人民共和国劳动法》第五十条，「工资应当以货币形式按月支付给劳动者本人」。",
        "工资怎么发？",
        EVIDENCE,
    ).ok
    kinds = _kinds("依据《中华人民共和国劳动法》第五十条，「工资可以由用人单位自行决定发放形式」。")
    assert kinds == ["unsupported_quote"]


def test_short_quotes_and_elided_quotes_are_skipped():
    """太短的片段到处都是；带省略号的本来就不是逐字引用——两者都放过，否则全是误报。"""
    assert _kinds("依据《中华人民共和国劳动法》第五十条，「工资」……「不得克扣」……") == []


def test_folded_whitespace_does_not_break_quote_matching():
    """PDF 折行空格（实测「民用航 空器」）不能影响比对。"""
    evidence = [
        _item(
            "中华人民共和国民用航空法",
            "87",
            "第八十七条　空中交通管制单位发现民用航 空器偏离指定航路时……",
        )
    ]
    answer = "依据《中华人民共和国民用航空法》第八十七条，「空中交通管制单位发现民用航空器偏离指定航路」。"
    assert verification.verify(answer, "偏离航路怎么办？", evidence).ok is True
