"""数据准入脱敏（设计 21.3）。

顺序是**先定位、后脱敏**：实体识别依赖上下文，先脱敏会破坏文本结构、导致定位失准。

入库的是**脱敏文本**；原文本不落库，由脱敏文本 + 映射表可逆重建（`restore`）。
映射表（`redaction_entities`）是系统内最敏感的数据，访问权限严于业务数据。
"""

from collections.abc import Iterable
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


def redact(text: str, detector: EntityDetector | None = None) -> tuple[str, list[RedactedEntity]]:
    """把识别到的实体替换为占位符，返回脱敏文本与映射条目。

    同一（类型, 原值）在本文本内复用同一占位符：既保证可逆重建，也让检索能把
    查询词转成同一个占位符（设计 21.3）。
    """
    detector = detector or RegexEntityDetector()
    assigned: dict[tuple[str, str], str] = {}
    counters: dict[str, int] = {}
    parts: list[str] = []
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
        cursor = entity.end

    parts.append(text[cursor:])
    entities = [
        RedactedEntity(entity_type, value, placeholder)
        for (entity_type, value), placeholder in assigned.items()
    ]
    return "".join(parts), entities


def restore(redacted_text: str, entities: Iterable[RedactedEntity]) -> str:
    """按映射还原原文本（设计 21.3：保留可逆映射）。

    长占位符先替换，避免 ``[案号_1]`` 命中 ``[案号_11]`` 的前缀。
    """
    restored = redacted_text
    for entity in sorted(entities, key=lambda item: len(item.placeholder), reverse=True):
        restored = restored.replace(entity.placeholder, entity.plaintext)
    return restored


async def apply_redaction(
    session: AsyncSession,
    principal: Principal,
    parse_revision_id: UUID,
    text: str,
    detector: EntityDetector | None = None,
) -> tuple[str, list[RedactedEntity]]:
    """脱敏并登记映射，返回脱敏文本。

    审计只记录条数与类型，**不记录明文**（设计 21.3、21.4）。
    """
    redacted_text, entities = redact(text, detector)
    async with session.begin():
        session.add_all(
            RedactionEntity(
                parse_revision_id=parse_revision_id,
                entity_type=entity.entity_type,
                plaintext=entity.plaintext,
                placeholder=entity.placeholder,
                created_by=principal.user_id,
            )
            for entity in entities
        )
        record_event(
            session,
            principal,
            parse_revision_id,
            "parse_revision.redacted",
            {
                "entity_count": len(entities),
                "entity_types": sorted({entity.entity_type for entity in entities}),
            },
        )
        await session.flush()
    return redacted_text, entities
