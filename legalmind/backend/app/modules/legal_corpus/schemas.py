"""法律版本的接口出入参（设计 §7、§13）。"""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class LegalVersionOut(BaseModel):
    """一个法律版本（供人工复核页展示）。

    ⚠️ **`legal_status` 与 `review_status` 是两件事**，页面上也要分开显示：

    - `legal_status`：**法律事实**（现行有效 / 已公布未生效 / 已废止 / 未知），由正文前言的
      公布信息推出。**人工复核改不了它**——把「已废止」点成「现行有效」正是 §8.3 要防的事。
    - `review_status`：**数据质量标记**（待审 / 已确认 / 已驳回），复核改的就是它。
    """

    id: UUID
    instrument_title: str
    version_label: str
    legal_status: str
    review_status: str
    promulgated_on: date | None = None
    effective_from: date | None = None
    #: 主原件文件名；允许先登记版本、后导入文件，所以可空
    artifact_filename: str | None = None
    created_at: datetime


class ReviewVersionRequest(BaseModel):
    """人工复核的结论。

    ⚠️ **只接受 `approved` / `rejected`**：`pending` 是「机器没把握」，不是人能做出的结论；
    要把版本退回待审就撤回原件重新导入（`withdraw-document` 会把状态置回 `pending`）。
    """

    decision: Literal["approved", "rejected"]
    note: str | None = Field(default=None, max_length=1000)
