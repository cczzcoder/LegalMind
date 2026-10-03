"""数据准入脱敏（设计 21.3）。

顺序是**先定位、后脱敏**：实体识别依赖上下文，先脱敏会破坏文本结构、导致定位失准。

入库的是**脱敏文本**；原文本不落库，由脱敏文本 + 映射表可逆重建（`restore`）。
映射表（`redaction_entities`）是系统内最敏感的数据，访问权限严于业务数据。
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Principal
from app.models import RedactionEntity
from app.modules.authorization.grants import record_event
from app.modules.redaction.detector import ENTITY_LABELS, EntityDetector, RegexEntityDetector

PLACEHOLDER_TEMPLATE = "[{label}_{index}]"


@dataclass(frozen=True)
class RedactedEntity:
    entity_type: str
    plaintext: str
    placeholder: str


@dataclass(frozen=True)
class RedactionSpan:
    """一次替换在**原文本**中的字符区间及其占位符。"""

    start: int
    end: int
    placeholder: str


def redact_spans(
    text: str,
    detector: EntityDetector | None = None,
) -> tuple[str, list[RedactedEntity], list[RedactionSpan]]:
    """脱敏并返回每次替换的位置。

    ``redact`` 只需要脱敏文本与映射；入库流水线还要按 chunk 区间取脱敏切片（设计 21.3
    的"先定位、后脱敏"），因此额外给出位置信息。
    """
    detector = detector or RegexEntityDetector()
    assigned: dict[tuple[str, str], str] = {}
    counters: dict[str, int] = {}
    parts: list[str] = []
    spans: list[RedactionSpan] = []
    cursor = 0

    for entity in detector.detect(text):
        # 与前一处命中重叠时跳过，避免重复替换与游标回退
        if entity.start < cursor:
            continue
        key = (entity.entity_type, entity.value)
        placeholder = assigned.get(key)
        if placeholder is None:
            counters[entity.entity_type] = counters.get(entity.entity_type, 0) + 1
            placeholder = PLACEHOLDER_TEMPLATE.format(
                label=ENTITY_LABELS.get(entity.entity_type, entity.entity_type),
                index=counters[entity.entity_type],
            )
            assigned[key] = placeholder
        parts.append(text[cursor : entity.start])
        parts.append(placeholder)
        spans.append(RedactionSpan(start=entity.start, end=entity.end, placeholder=placeholder))
        cursor = entity.end

    parts.append(text[cursor:])
    entities = [
        RedactedEntity(entity_type, value, placeholder)
        for (entity_type, value), placeholder in assigned.items()
    ]
    return "".join(parts), entities, spans


def redact(text: str, detector: EntityDetector | None = None) -> tuple[str, list[RedactedEntity]]:
    """把识别到的实体替换为占位符，返回脱敏文本与映射条目。

    同一（类型, 原值）在本文本内复用同一占位符：既保证可逆重建，也让检索能把
    查询词转成同一个占位符（设计 21.3）。
    """
    redacted_text, entities, _ = redact_spans(text, detector)
    return redacted_text, entities


def redacted_slice(
    text: str,
    spans: Sequence[RedactionSpan],
    start: int,
    end: int,
) -> str:
    """取原文本 ``[start, end)`` 的脱敏形态（设计 21.3）。

    入库的是脱敏文本（``chunks.text``），而分块边界与 ``chunk_spans`` 偏移都在**原文本**
    坐标上，所以要按原文本区间取切片。

    跨区间边界的实体：占位符归**起点所在**的区间，后续区间不再输出它的尾部——任何一段区间
    都不会漏出明文，且占位符只出现一次。区间之间不含被实体吞掉的字符时（正常情形），
    各区间脱敏文本按 ``"\\n"`` 连接即整篇脱敏文本；当前正则识别器不匹配换行符，而 chunk
    边界恰好落在块分隔符上，因此实体不会跨边界，该等式在实际流水线中恒成立。
    """
    parts: list[str] = []
    cursor = start
    for span in spans:
        if span.end <= start:
            continue
        if span.start >= end:
            break
        if span.start >= start:
            parts.append(text[cursor : span.start])
            parts.append(span.placeholder)
        cursor = max(cursor, span.end)
    if cursor < end:
        parts.append(text[cursor:end])
    return "".join(parts)


def restore(redacted_text: str, entities: Iterable[RedactedEntity]) -> str:
    """按映射还原原文本（设计 21.3：保留可逆映射）。

    长占位符先替换，避免 ``[案号_1]`` 命中 ``[案号_11]`` 的前缀。
    """
    restored = redacted_text
    for entity in sorted(entities, key=lambda item: len(item.placeholder), reverse=True):
        restored = restored.replace(entity.placeholder, entity.plaintext)
    return restored


def persist_redaction(
    session: AsyncSession,
    principal: Principal,
    parse_revision_id: UUID,
    entities: Iterable[RedactedEntity],
) -> None:
    """把映射写入**调用方的事务**（设计 12.1：与业务变更同事务提交）。

    审计只记录条数与类型，**不记录明文**（设计 21.3、21.4）。即使没有命中实体也留痕，
    以便核对"脱敏节点确实执行过"。
    """
    items = list(entities)
    session.add_all(
        RedactionEntity(
            parse_revision_id=parse_revision_id,
            entity_type=entity.entity_type,
            plaintext=entity.plaintext,
            placeholder=entity.placeholder,
            created_by=principal.user_id,
        )
        for entity in items
    )
    record_event(
        session,
        principal,
        parse_revision_id,
        "parse_revision.redacted",
        {
            "entity_count": len(items),
            "entity_types": sorted({entity.entity_type for entity in items}),
        },
    )


async def apply_redaction(
    session: AsyncSession,
    principal: Principal,
    parse_revision_id: UUID,
    text: str,
    detector: EntityDetector | None = None,
) -> tuple[str, list[RedactedEntity]]:
    """脱敏并登记映射，返回脱敏文本。

    自带事务，适合独立调用；入库流水线应改用 ``redact_spans`` + ``persist_redaction``，
    把映射与分块写在同一个事务里。
    """
    redacted_text, entities = redact(text, detector)
    async with session.begin():
        persist_redaction(session, principal, parse_revision_id, entities)
        await session.flush()
    return redacted_text, entities
