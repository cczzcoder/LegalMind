"""实体识别接口与正则基线实现（设计 21.3）。

按设计第 2 节的适配层约定，识别器是**可替换实现**，经样本测试后锁定。
正则只能覆盖有固定格式的标识符；当事人姓名等需要上下文的类型，待模型选型
后作为第二个实现接入（第 9.5 节：模型按需启动，不默认常驻）。
"""

import re
from dataclasses import dataclass
from typing import Protocol

# 实体类型；与 redaction_entities.entity_type 的 CHECK 约束一致
ENTITY_TYPES = ("id_number", "case_number", "phone", "person")

# 占位符中展示的中文标签
ENTITY_LABELS = {
    "id_number": "身份证",
    "case_number": "案号",
    "phone": "电话",
    "person": "当事人",
}


@dataclass(frozen=True)
class DetectedEntity:
    """一次命中：类型、原文、在文本中的字符区间。"""

    entity_type: str
    value: str
    start: int
    end: int


class EntityDetector(Protocol):
    def detect(self, text: str) -> list[DetectedEntity]: ...


# 身份证号：18 位，末位可为 X
_ID_NUMBER = re.compile(r"\d{17}[\dXx]")
# 案号：（2023）京01民终1234号，全角或半角括号
_CASE_NUMBER = re.compile(r"[（(]\d{4}[）)][\u4e00-\u9fa5A-Za-z0-9]{1,20}?号")
# 手机号：11 位，1 开头
_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")


class RegexEntityDetector:
    """按固定格式匹配标识符；不识别需要上下文的姓名。"""

    _PATTERNS = (
        ("id_number", _ID_NUMBER),
        ("case_number", _CASE_NUMBER),
        ("phone", _PHONE),
    )

    def detect(self, text: str) -> list[DetectedEntity]:
        found = [
            DetectedEntity(entity_type, match.group(), match.start(), match.end())
            for entity_type, pattern in self._PATTERNS
            for match in pattern.finditer(text)
        ]
        # 按起点排序，供替换时按顺序推进游标
        found.sort(key=lambda entity: (entity.start, entity.end))
        return found
