"""§9.2 的结构化输出：解析容错、渲染、结构化核验。

**不连数据库、不加载模型**——都是纯函数。
"""

from dataclasses import dataclass

import pytest

from app.modules.answering import claims, verification


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
        "50",
        "第五十条",
        "unknown",
        "第五十条　工资应当以货币形式按月支付给劳动者本人。",
    ),
    _Item(
        "中华人民共和国劳动法",
        "100",
        "第一百条",
        "unknown",
        "第一百条　用人单位无故不缴纳社会保险费的……",
    ),
]


def test_parses_a_clean_structured_answer():
    parsed = claims.parse(
        '{"claims":[{"claim_id":"c1","text":"工资应当以货币形式支付","evidence_ids":["1"]}],'
        '"missing_facts":[],"conflicts":[]}'
    )
    assert parsed.ok and parsed.repaired is False
    assert parsed.answer.claims[0].text == "工资应当以货币形式支付"
    assert parsed.answer.cited_evidence_ids == ("1",)


def test_parses_json_wrapped_in_a_code_fence():
    parsed = claims.parse('```json\n{"claims":[{"text":"结论","evidence_ids":["1"]}]}\n```')
    assert parsed.ok and parsed.repaired is False


def test_repairs_two_concatenated_json_values():
    """实测：即使开了 Ollama 的 JSON 约束，模型仍可能输出 `[]` 与 `{...}` 两段并列。

    `json.loads` 会报 `Extra data`——**容错解析是必需的**，但**修过要记下来**（`repaired`），
    否则「模型到底有多规矩」就看不见了。
    """
    parsed = claims.parse(
        '[]\n\n{"missing_facts":[{"fact_id":"f1","text":"条文中未提及相关内容"}]}'
    )
    assert parsed.ok
    assert parsed.repaired is True
    assert parsed.answer.abstained is True
    assert parsed.answer.missing_facts == ["条文中未提及相关内容"]


def test_missing_facts_accepts_plain_strings_and_objects():
    plain = claims.parse('{"claims":[],"missing_facts":["还缺甲"]}')
    objects = claims.parse('{"claims":[],"missing_facts":[{"fact_id":"f1","text":"还缺甲"}]}')
    assert plain.answer.missing_facts == objects.answer.missing_facts == ["还缺甲"]


def test_evidence_ids_are_normalised_to_strings():
    """模型可能写数字编号（1）而不是字符串（"1"）——不归一化，核验会整片误报。"""
    parsed = claims.parse('{"claims":[{"text":"结论","evidence_ids":[1,2]}]}')
    assert parsed.answer.claims[0].evidence_ids == ["1", "2"]


def test_unparseable_output_is_reported_not_guessed():
    parsed = claims.parse("根据条文，工资应当以货币形式支付。")
    assert parsed.ok is False
    assert parsed.error


def test_empty_output_is_reported():
    assert claims.parse("").ok is False
    assert claims.parse("   ").ok is False


def test_render_puts_server_generated_citations_on_each_claim():
    """§9.2：**服务端负责生成引用标题**——模型只回填编号，标题从这里取。"""
    parsed = claims.parse(
        '{"claims":[{"text":"工资应当以货币形式按月支付","evidence_ids":["1"],'
        '"conditions":["存在劳动关系"],"limitations":["未涉及实物发放"]}]}'
    )
    text = claims.render(parsed.answer, {"1": "《中华人民共和国劳动法》第五十条"})
    assert "工资应当以货币形式按月支付" in text
    assert "《中华人民共和国劳动法》第五十条" in text
    assert "前提：存在劳动关系" in text
    assert "未确认：未涉及实物发放" in text


def test_render_of_an_abstention_says_what_is_missing():
    parsed = claims.parse('{"claims":[],"missing_facts":["还缺工伤认定的条文"]}')
    text = claims.render(parsed.answer, {})
    assert "依据不足" in text
    assert "还缺工伤认定的条文" in text


def test_verify_claims_rejects_an_evidence_id_outside_the_set():
    parsed = claims.parse('{"claims":[{"text":"结论","evidence_ids":["9"]}]}')
    result = verification.verify_claims(parsed.answer, "问题", EVIDENCE)
    assert result.ok is False
    assert [issue.kind for issue in result.issues] == ["unwarranted_evidence"]


def test_verify_claims_rejects_a_claim_without_any_evidence_id():
    """§9.2「模型仅输出结构化主张**和允许的证据 ID**」：没有编号的主张就是没有依据。"""
    parsed = claims.parse('{"claims":[{"text":"凭印象下的结论","evidence_ids":[]}]}')
    result = verification.verify_claims(parsed.answer, "问题", EVIDENCE)
    assert result.ok is False
    assert [issue.kind for issue in result.issues] == ["unsupported_claim"]


def test_verify_claims_still_checks_the_claim_text():
    """结构化之后**文本层检查仍然要跑**——主张正文本身也可能写出证据外的条号。"""
    parsed = claims.parse(
        '{"claims":[{"text":"依据《中华人民共和国劳动法》第一百零一条……","evidence_ids":["1"]}]}'
    )
    result = verification.verify_claims(parsed.answer, "问题", EVIDENCE)
    assert result.ok is False
    assert "unwarranted_article" in [issue.kind for issue in result.issues]


def test_verify_claims_passes_a_sound_answer():
    parsed = claims.parse(
        '{"claims":[{"text":"工资应当以货币形式按月支付给劳动者本人","evidence_ids":["1"]}]}'
    )
    assert verification.verify_claims(parsed.answer, "工资怎么发？", EVIDENCE).ok is True


@pytest.mark.parametrize("raw", ["null", "123", '"一段字符串"'])
def test_non_object_json_is_reported(raw):
    """顶层不是对象（`null` / 数字 / 字符串）时不能崩，也不能硬当成有结论。"""
    parsed = claims.parse(raw)
    assert parsed.ok is False
