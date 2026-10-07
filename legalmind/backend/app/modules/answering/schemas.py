"""问答接口的输入输出（设计 §9.1、§9.4、§13）。

⚠️ **回答正文不在这些模型里落库**：`AnswerRunOut.answer` 是从**短期缓存**里取出来拼上去的，
不是数据库字段（§21「客户数据不落库」）。取不到就是 `content_available=false` + `answer=null`，
**如实告知，不拿别的东西冒充**。
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class SubmitQuestion(BaseModel):
    """提交一次异步问答（设计 §9.4）。"""

    question: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=5, ge=1, le=50, description="取多少条依据")
    max_new_tokens: int = Field(default=512, ge=64, le=4096)
    model: str | None = Field(default=None, description="覆盖生成模型（默认取配置里的）")


class ClarifyRequest(BaseModel):
    """回答澄清问题、继续同一个运行（设计 §9.1 的 `CLARIFYING → RETRIEVING`）。"""

    supplement: str = Field(min_length=1, max_length=2000)


class ReviewRequest(BaseModel):
    """人工复核的结论（设计 §9.3 第三层）。"""

    note: str | None = Field(default=None, max_length=2000)


class AnswerRunOut(BaseModel):
    """一次问答运行。**内容来自短期缓存**，见模块 docstring。"""

    id: UUID
    state: str
    previous_state: str | None
    published: bool
    blocked_by: str | None
    review_required: bool
    #: 复核人（未复核为 None）。**前端要显示「谁复核的」**——审计要的也是这个。
    reviewed_by: UUID | None
    reviewed_at: datetime | None
    review_note: str | None
    evidence_count: int
    seconds: float | None
    created_at: datetime

    #: 是不是在等用户补充（§9.1 `CLARIFYING`）——此时 `answer` 里是**澄清问题**，不是结论
    clarifying: bool = False
    #: 短期缓存里还有没有内容（TTL 到期、未配置缓存、或没产出 → false）
    content_available: bool
    question: str | None = None
    answer: str | None = None
