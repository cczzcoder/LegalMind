"""法律版本的人工复核（需求第 3 节「法律审核员：审核资料版本、Wiki 和待审回答」）。

**为什么需要它**：`legal_versions.review_status` 原先只有**自动**写入——落库时按置信度判
`approved`（正文完整、无回退、无多原件冲突）或 `pending`（设计 §5.1、§7）。低置信度的版本被置成
`pending` 之后，**没有任何路径能把它改掉**：CLI、API、界面都没有，只能连库改 SQL。需求第 3 节
写着法律审核员要「审核资料版本」，这是缺掉的那一半（设计 §6 也如实记着「人工确认环节本身仍未
实现」）。Wiki 的待审队列（V1.15）与问答运行的复核（V1.25）都补齐了，只剩这一条。

**为什么单独一个文件**：`service.py` 管的是「解析产物怎么挂成版本树」——自动落库那条路。
人工复核是**之后发生的另一件事**，与 `answering/runs.py::mark_reviewed` 同一性质。

**三条口径**：

- **只改审核状态，绝不动效力状态。** `legal_status` 是**法律事实**（由正文前言的主席令与
  「自…起施行」推出，设计 §5.1），`review_status` 是**数据质量标记**。人工复核不能把「已废止」
  点成「现行有效」——那正是本系统要防的事（§8.3 靠效力状态屏蔽失效版本）。
- **允许改已复核的版本**（这一点与 `mark_reviewed` 刻意不同）。问答运行是**历史记录**，复核一次
  就定了；而 `review_status` 是**当前状态**，审错了必须能纠正——否则只是把「只能改 SQL」换个
  地方再来一遍。每次改动都写审计，留痕在审计里。
- **不新增列**。`legal_versions` 上没有 `reviewed_by` / `reviewed_at`（`answer_runs` 有）。
  「谁在什么时候复核的」由**审计表**回答（设计 §21.2 就是干这个的）；等真要做界面时再加
  反规范化列也不迟——现在加，等于为还不存在的读法先付一次迁移成本。
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Principal
from app.models import LegalInstrument, LegalVersion, SourceArtifact
from app.modules.authorization.grants import record_audit

#: 人工能做出的两种结论。**`pending` 不在其中**——它是「机器没把握」，不是人能做出的判断；
#: 要把版本退回待审就撤回原件重新导入（`withdraw-document` 会把状态置回 `pending`）。
REVIEW_DECISIONS = ("approved", "rejected")


async def review_version(
    session: AsyncSession,
    principal: Principal,
    version_id: UUID,
    *,
    decision: str,
    note: str | None = None,
) -> LegalVersion:
    """把某个法律版本的 `review_status` 置为 `approved` / `rejected`。

    **调用方负责事务与权限**（CLI 走 `cli_principal(..., REVIEW_DECIDE)`）。

    抛 `ValueError`：结论不认识，或版本**已经是**该结论（避免无意义的空转，也免得审计里堆一串
    什么也没改变的记录）；抛 `LookupError`：版本不存在。
    """
    if decision not in REVIEW_DECISIONS:
        raise ValueError(f"不认识的复核结论：{decision}（可选 {' / '.join(REVIEW_DECISIONS)}）")
    version = await session.get(LegalVersion, version_id)
    if version is None:
        raise LookupError("法律版本不存在")
    if version.review_status == decision:
        raise ValueError(f"该版本已经是 {decision}，没有需要改的")

    previous = version.review_status
    version.review_status = decision
    record_audit(
        session,
        principal,
        version.id,
        "legal_version.reviewed",
        {
            "from": previous,
            "to": decision,
            "note": note,
            # 效力状态一并记下来：复核**不该**动它，记进去是为了以后能从审计里证明这一点
            "legal_status": version.legal_status,
        },
    )
    await session.flush()
    return version


async def pending_versions(session: AsyncSession, *, limit: int = 20) -> list[tuple]:
    """列出待人工复核的版本（`review_status='pending'`），最近导入的在前。

    返回 `(version, instrument_title, artifact_filename)`——CLI 要打给操作者看，
    光有 id 认不出是哪部法律的哪一版。原件用**外连接**：允许先登记版本、后导入文件
    （`artifact_id` 可空）。
    """
    rows = await session.execute(
        select(LegalVersion, LegalInstrument.title, SourceArtifact.original_filename)
        .join(LegalInstrument, LegalInstrument.id == LegalVersion.instrument_id)
        .outerjoin(SourceArtifact, SourceArtifact.id == LegalVersion.artifact_id)
        .where(LegalVersion.review_status == "pending")
        .order_by(LegalVersion.created_at.desc())
        .limit(limit)
    )
    return list(rows.all())
