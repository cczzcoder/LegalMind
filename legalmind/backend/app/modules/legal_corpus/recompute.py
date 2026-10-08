"""按日期重算法律版本的效力状态（设计 §5.1、§5.3、§8.3）。

**为什么需要它**：`legal_status` 原先**只在落库时算一次**——`metadata._legal_status` 拿落库当天的
日期比一比，`effective_from > today` 就是 `not_yet_effective`。时间往前走，这个结论会过期，
而**检索默认屏蔽 `not_yet_effective`**（§8.3）。于是「今天开始施行的法律」会**静默地从检索里
消失**，不报错、不留痕、没有任何提示。

实测（2026-10-08）：监狱法 `effective_from = 2026-11-01`，到那天它仍然读作未生效，检索里就没有它了。
设计文档 §5.1 把这条记为「需要重新关联或定期重算，尚未实现」，这里补上。

**两条口径**：

- **只做日期能证明的事**。只动 `effective_from` 明确且已到期的版本；`effective_from` 为 NULL
  （判定不了）的一律不动——这正是 §5.3「日期未知即 NULL 不虚构」的要求，与 `metadata` 里
  `unknown` 的判法同源。取代规则同理：`promulgated_on` 缺失的不参与比较。
- **不改 `review_status`**。那是**数据质量标记**（这条元数据被人确认过没有），与时间无关；
  重算只回答「今天它算不算现行有效」，不替人做确认（见 `review.py` 的分工）。

⚠️ **不是定时任务**：本项目不引入调度器（与「没有测量支撑的组件不装」同一口径）。这是个
**维护动作**，由 `app.cli recompute-statuses` 手动跑——建议每次导入新语料后跑一次，
以及在有版本临近施行日期时跑一次。

⚠️ `today` 由调用方传入，**用 UTC 日期**（与落库路径 `parsing/service.py` 的
`datetime.now(UTC).date()` 一致）。施行日期本是本地日期概念，两边都按 UTC 最多差不到一天，
但**两边必须一致**——否则同一条法律在落库时与重算时会得出不同结论。
"""

from dataclasses import dataclass, field
from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Principal
from app.models import LegalInstrument, LegalVersion
from app.modules.authorization.grants import record_event
from app.modules.legal_corpus.metadata import EFFECTIVE, NOT_YET_EFFECTIVE, REPEALED


@dataclass
class RecomputeReport:
    """这次重算做了什么（或**会**做什么——`dry_run=True` 时）。

    条目里都带上**法律名称**：光有 id，运维的人认不出是哪部法律的哪一版。
    """

    today: date
    dry_run: bool
    instruments: int = 0
    #: (版本 id, 法律名称, 版本标识, 施行日期)
    became_effective: list[tuple[UUID, str, str, date]] = field(default_factory=list)
    #: (版本 id, 法律名称, 版本标识, 取代它的版本 id)
    repealed: list[tuple[UUID, str, str, UUID]] = field(default_factory=list)


def plan(versions: list[LegalVersion], today: date) -> tuple[list[LegalVersion], list[tuple]]:
    """**纯函数**：算出这次重算会改哪些状态。预演与实际执行共用同一份判断。

    返回 `(转为现行有效的版本, [(被取代的版本, 取代它的版本), …])`。

    ⚠️ 取代用的是**严格早于**（`promulgated_on <`），与落库路径的 `repeal_superseded` 一致：
    **同日公布的两个版本不算互相取代**——那是「同日法律版本比较」要处理的事，不是这里猜的。
    """
    becomes_effective = [
        version
        for version in versions
        if version.legal_status == NOT_YET_EFFECTIVE
        and version.effective_from is not None
        and version.effective_from <= today
    ]

    # 先算出「改完之后谁现行有效」，再让公布最晚的那个去取代更早的
    in_force = [v for v in versions if v.legal_status == EFFECTIVE] + becomes_effective
    dated = [v for v in in_force if v.promulgated_on is not None]
    if not dated:
        return becomes_effective, []
    newest = max(dated, key=lambda v: v.promulgated_on)
    # ⚠️ 用 `is not` 而不是比 id：同一个 session 里同一行只会有一个实例（identity map），
    # 而且这样**不依赖主键已生成**——`plan()` 才能拿纯对象直接做单元测试。
    superseded = [
        (version, newest)
        for version in dated
        if version is not newest and version.promulgated_on < newest.promulgated_on
    ]
    return becomes_effective, superseded


async def recompute_statuses(
    session: AsyncSession,
    principal: Principal,
    *,
    today: date,
    dry_run: bool = False,
) -> RecomputeReport:
    """重算受影响的那些法律版本的效力状态。**调用方负责事务与权限。**

    ⚠️ **只扫「有待到期版本的法律」**，不做全库重扫：全库重扫等于把落库时的判断再跑一遍，
    而落库路径有它自己的上下文（多原件择优、冲突合并），两套逻辑并存必然漂。这里只补
    「时间往前走」这一件落库时做不到的事。
    """
    instrument_ids = list(
        await session.scalars(
            select(LegalVersion.instrument_id)
            .distinct()
            .where(
                LegalVersion.legal_status == NOT_YET_EFFECTIVE,
                LegalVersion.effective_from.is_not(None),
                LegalVersion.effective_from <= today,
            )
        )
    )
    report = RecomputeReport(today=today, dry_run=dry_run, instruments=len(instrument_ids))

    for instrument_id in instrument_ids:
        versions = list(
            await session.scalars(
                select(LegalVersion)
                .where(LegalVersion.instrument_id == instrument_id)
                .order_by(LegalVersion.promulgated_on, LegalVersion.id)
            )
        )
        title = await session.scalar(
            select(LegalInstrument.title).where(LegalInstrument.id == instrument_id)
        )
        becomes_effective, superseded = plan(versions, today)

        if not dry_run:
            for version in becomes_effective:
                version.legal_status = EFFECTIVE
                record_event(
                    session,
                    principal,
                    version.id,
                    "legal_version.became_effective",
                    {
                        "legal_version_id": str(version.id),
                        "instrument_id": str(instrument_id),
                        "effective_from": version.effective_from.isoformat(),
                        # 记下「谁把它算成有效的」——将来出问题要能回溯到这次重算
                        "recomputed_on": today.isoformat(),
                    },
                )
            for older, newer in superseded:
                older.legal_status = REPEALED
                record_event(
                    session,
                    principal,
                    older.id,
                    "legal_version.repealed",
                    {
                        "legal_version_id": str(older.id),
                        "repealed_by_version_id": str(newer.id),
                        "instrument_id": str(instrument_id),
                        "recomputed_on": today.isoformat(),
                    },
                )
            await session.flush()

        report.became_effective.extend(
            (version.id, title, version.version_label, version.effective_from)
            for version in becomes_effective
        )
        report.repealed.extend(
            (older.id, title, older.version_label, newer.id) for older, newer in superseded
        )

    return report
