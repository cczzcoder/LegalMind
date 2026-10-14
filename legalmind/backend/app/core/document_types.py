"""文档领域的共享契约：媒体类型、文本块、文本哈希（设计 §4、§6、§7.1）。

⚠️ **为什么这些符号放在 `app.core`**：它们被三个业务模块同时使用——
`documents`（登记时判媒体类型）、`parsing`（解析与分块）、`legal_corpus`（结构检测与条款落库）。
此前它们分散在 `documents.validation` / `documents.service` / `parsing.interface` /
`parsing.chunking` 里，于是形成了
`legal_corpus → parsing → documents → legal_corpus` 的**循环依赖**（V1.51 断开）。
`app.core` 是唯一能同时被三者依赖的位置——它不依赖任何业务模块。

⚠️ **原位置改为从本模块导入并继续导出**，因此既有导入路径（含测试里的）全部保持有效；
变的只是模块之间的依赖方向。新增引用请**直接从本模块导入**。
"""

import hashlib
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# 媒体类型：文件内容判定与解析后端选择共用
# ---------------------------------------------------------------------------

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF = "application/pdf"
HTML = "text/html"
TEXT = "text/plain"

# 解析处理配置版本；解析器选定后随配置变化递增（设计 7.1）
PARSE_CONFIG_VERSION = "v1"


# ---------------------------------------------------------------------------
# 规范化文本块
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Block:
    """规范化文本中的一个块，带其在原件中的定位（设计 §6）。

    ``bbox`` 为归一化页面坐标 ``(x0, y0, x1, y1)``；无版面概念的格式（HTML、TXT）为 ``None``，
    此时 ``coordinate_system`` 也为 ``None``（数据库约束要求两者同时有或同时无）。
    """

    ordinal: int
    kind: str
    text: str
    char_start: int
    char_end: int
    page_index: int | None = None
    printed_page_label: str | None = None
    block_id: str | None = None
    coordinate_system: str | None = None
    bbox: tuple[float, float, float, float] | None = None
    level: int | None = None


def text_sha256(text: str) -> str:
    """脱敏前原文的 sha256；chunk 还原后应与 ``ParseRevision.text_sha256`` 一致。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
