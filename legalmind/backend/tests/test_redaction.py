"""数据准入脱敏（设计 21.3）：实体识别、占位符稳定性、可逆重建与映射表。

单元部分不依赖数据库；映射表部分需要 TEST_DATABASE_URL。
"""

import json

import pytest
from sqlalchemy import select

from app.core.security import Principal
from app.models import AuditEvent, RedactionEntity
from app.modules.redaction.detector import DetectedEntity, RegexEntityDetector
from app.modules.redaction.service import (
    RedactedEntity,
    RedactionSpan,
    apply_redaction,
    redact,
    redact_spans,
    redacted_slice,
    restore,
)
from tests.helpers import seed_parse_chain

SAMPLE = "身份证110101199001011234，案号（2023）京01民终1234号，电话13800138000。"


def test_detects_formatted_identifiers():
    types = sorted({entity.entity_type for entity in RegexEntityDetector().detect(SAMPLE)})
    assert types == ["case_number", "id_number", "phone"]


def test_does_not_flag_ordinary_legal_text():
    assert (
        RegexEntityDetector().detect("第一条　为了保护劳动者的合法权益，根据宪法，制定本法。") == []
    )


def test_placeholder_is_stable_for_repeated_value():
    redacted, entities = redact("原告110101199001011234，被告亦为110101199001011234。")
    assert redacted == "原告[身份证_1]，被告亦为[身份证_1]。"
    assert len(entities) == 1


def test_placeholders_are_numbered_per_type():
    redacted, entities = redact("案号（2023）京01民终1234号与（2024）沪02民终5678号。")
    assert "[案号_1]" in redacted and "[案号_2]" in redacted
    assert len(entities) == 2


def test_redaction_is_idempotent():
    assert redact(SAMPLE)[0] == redact(SAMPLE)[0]


def test_restore_rebuilds_the_original_text():
    redacted, entities = redact(SAMPLE)
    assert redacted != SAMPLE
    assert restore(redacted, entities) == SAMPLE


def test_restore_does_not_confuse_placeholder_prefixes():
    """[案号_1] 不得命中 [案号_11] 的前缀。"""
    entities = [
        RedactedEntity("case_number", "甲案", "[案号_1]"),
        RedactedEntity("case_number", "乙案", "[案号_11]"),
    ]
    assert restore("[案号_1] 与 [案号_11]", entities) == "甲案 与 乙案"


def test_overlapping_matches_are_replaced_once():
    """识别器给出重叠命中时只替换第一处，不重复替换也不回退游标。"""

    class Overlapping:
        def detect(self, text):
            return [
                DetectedEntity("id_number", text[0:10], 0, 10),
                DetectedEntity("phone", text[5:15], 5, 15),
            ]

    redacted, entities = redact("0123456789abcdef", Overlapping())
    assert redacted == "[身份证_1]abcdef"
    assert len(entities) == 1


# ---------------------------------------------------------------------------
# 按原文本区间取脱敏切片（设计 21.3：入库脱敏文本、偏移指向原文本）
# ---------------------------------------------------------------------------


def test_redact_spans_reports_replaced_ranges():
    redacted, entities, spans = redact_spans(SAMPLE)
    assert redacted == redact(SAMPLE)[0]
    assert [(span.start, span.end) for span in spans] == [
        (3, 21),  # 110101199001011234
        (24, 40),  # （2023）京01民终1234号
        (43, 54),  # 13800138000
    ]
    # 每个 span 的区间确实等于它替换掉的原值
    assert [SAMPLE[span.start : span.end] for span in spans] == [
        entity.plaintext for entity in entities
    ]
    assert all(SAMPLE[span.start : span.end] != span.placeholder for span in spans)


def test_redacted_slice_reassembles_the_redacted_text():
    """按块区间切片再拼接，等于整篇脱敏文本（流水线的实际用法）。"""
    text = "身份证110101199001011234\n第二条　联系电话13800138000"
    _, _, spans = redact_spans(text)
    # 块边界：第一块 [0, 21)，第二块 [22, 41)
    pieces = [redacted_slice(text, spans, 0, 21), redacted_slice(text, spans, 22, 41)]
    assert pieces == ["身份证[身份证_1]", "第二条　联系电话[电话_1]"]
    assert "\n".join(pieces) == redact(text)[0]


def test_redacted_slice_without_entities_returns_the_original_slice():
    text = "第一条　没有个人信息的条文\n第二条　也没有"
    assert redacted_slice(text, [], 0, 12) == text[0:12]
    assert redacted_slice(text, [], 13, 19) == text[13:19]


def test_straddling_span_never_leaks_plaintext():
    """实体跨区间边界（当前识别器不会产生，属防御性行为）：占位符只出现一次，且不漏明文。

    区间之间含被实体吞掉的字符时，拼接结果不再等于整篇脱敏文本——这是边界情形的固有代价，
    但**任何一段区间都不会漏出明文**，这是必须守住的一条。
    """
    text = "abcde\nfghij"
    spans = [RedactionSpan(start=3, end=8, placeholder="[X]")]  # 吞掉 "de\nfg"

    left = redacted_slice(text, spans, 0, 5)
    right = redacted_slice(text, spans, 6, 11)

    assert left == "abc[X]"
    assert right == "hij"
    assert "defg" not in left + right
    assert (left + right).count("[X]") == 1


@pytest.mark.anyio
async def test_apply_redaction_persists_mapping_without_plaintext_in_audit(session_factory):
    text = "身份证110101199001011234，案号（2023）京01民终1234号。"

    async with session_factory() as session:
        _, user, _, _, parse_revision, _ = await seed_parse_chain(session)
        await session.commit()
        principal = Principal(
            organization_id=user.organization_id,
            user_id=user.id,
            roles=frozenset({"editor"}),
        )
        redacted, entities = await apply_redaction(session, principal, parse_revision.id, text)

    assert "[身份证_1]" in redacted and "[案号_1]" in redacted
    assert len(entities) == 2

    async with session_factory() as session:
        rows = list(
            await session.scalars(
                select(RedactionEntity).where(
                    RedactionEntity.parse_revision_id == parse_revision.id
                )
            )
        )
        assert {row.placeholder for row in rows} == {"[身份证_1]", "[案号_1]"}
        assert {row.plaintext for row in rows} == {"110101199001011234", "（2023）京01民终1234号"}

        audit = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "parse_revision.redacted",
                AuditEvent.resource_id == parse_revision.id,
            )
        )

    # 审计只记元数据：明文不得出现在审计正文里（设计 21.3、21.4）
    payload = json.dumps(audit.payload, ensure_ascii=False)
    assert audit.payload["entity_count"] == 2
    assert "110101199001011234" not in payload
    assert "京01民终1234号" not in payload
