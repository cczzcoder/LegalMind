"""按日期重算效力状态（`legal_corpus/recompute.py`；设计 §5.1、§5.3、§8.3）。

**为什么单独钉**：`legal_status` 原先只在落库时算一次，而**检索默认屏蔽 `not_yet_effective`**
——「今天开始施行的法律」不重算就会**静默地从检索里消失**，不报错也不留痕。这里钉三件事：

1. **只做日期能证明的事**：`effective_from` / `promulgated_on` 缺失的一律不动（§5.3 不虚构）；
2. **取代用的是严格早于**：同日公布的两个版本不算互相取代；
3. **不碰 `review_status`**：那是数据质量标记，与时间无关。

⚠️ **断言只针对本用例自己造的版本，不查全库计数**：`recompute_statuses` 是**全库扫描**，
而测试库跨用例、跨运行累积（例如 dry-run 那条会故意留下一条「已到期但没改」的版本）。
用 `report.instruments == N` 这种全局计数会随执行顺序飘。
"""

from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.security import Principal
from app.models import AuditEvent, LegalInstrument, LegalVersion, OutboxEvent
from app.modules.legal_corpus import recompute

pytestmark = pytest.mark.anyio

TODAY = date(2026, 11, 15)


def _principal(user) -> Principal:
    return Principal(organization_id=user.organization_id, user_id=user.id, roles=frozenset())


def _version(
    *,
    legal_status: str,
    promulgated_on: date | None,
    effective_from: date | None,
    review_status: str = "approved",
) -> LegalVersion:
    """**纯内存对象**——`plan()` 不依赖主键，所以单元测试不必碰数据库。"""
    return LegalVersion(
        instrument_id=uuid4(),
        version_label="2026年修订",
        legal_status=legal_status,
        promulgated_on=promulgated_on,
        effective_from=effective_from,
        review_status=review_status,
    )


def test_plan_moves_due_versions_to_effective():
    due = _version(
        legal_status="not_yet_effective", promulgated_on=date(2026, 4, 30), effective_from=TODAY
    )
    later = _version(
        legal_status="not_yet_effective",
        promulgated_on=date(2026, 6, 26),
        effective_from=date(2027, 1, 1),
    )
    becomes_effective, superseded = recompute.plan([due, later], TODAY)
    assert [v is due for v in becomes_effective] == [True]
    assert superseded == []


def test_plan_leaves_undated_versions_alone():
    """§5.3 不虚构：日期未知就判不了，判不了就不动。"""
    no_date = _version(legal_status="not_yet_effective", promulgated_on=None, effective_from=None)
    becomes_effective, superseded = recompute.plan([no_date], TODAY)
    assert becomes_effective == []
    assert superseded == []


def test_plan_repeals_the_older_effective_version():
    """「旧版本随之被取代」：新的生效之后，公布更早的那个不再现行有效。"""
    old = _version(
        legal_status="effective", promulgated_on=date(2018, 12, 29), effective_from=date(2019, 1, 1)
    )
    new = _version(
        legal_status="not_yet_effective", promulgated_on=date(2026, 4, 30), effective_from=TODAY
    )
    becomes_effective, superseded = recompute.plan([old, new], TODAY)
    assert [v is new for v in becomes_effective] == [True]
    assert [(o is old, n is new) for o, n in superseded] == [(True, True)]


def test_plan_does_not_repeal_same_day_versions():
    """同日公布的两个版本不算互相取代——那是「同日法律版本比较」的事，不是这里猜的。"""
    first = _version(
        legal_status="effective", promulgated_on=date(2026, 4, 30), effective_from=date(2026, 7, 1)
    )
    second = _version(
        legal_status="not_yet_effective", promulgated_on=date(2026, 4, 30), effective_from=TODAY
    )
    _becomes, superseded = recompute.plan([first, second], TODAY)
    assert superseded == []


def test_plan_leaves_unknown_promulgation_alone():
    """公布日期缺失时不参与取代比较——判不了先后就不动（§5.3）。"""
    undated = _version(
        legal_status="effective", promulgated_on=None, effective_from=date(2019, 1, 1)
    )
    new = _version(
        legal_status="not_yet_effective", promulgated_on=date(2026, 4, 30), effective_from=TODAY
    )
    _becomes, superseded = recompute.plan([undated, new], TODAY)
    assert superseded == []


async def _instrument(session_factory, user, versions: list[dict]):
    async with session_factory() as session, session.begin():
        instrument = LegalInstrument(
            title=f"示例测试法（{uuid4().hex[:8]}）",
            jurisdiction="中国",
            issuing_body="全国人民代表大会常务委员会",
            instrument_type="law",
            created_by=user.id,
        )
        session.add(instrument)
        await session.flush()
        rows = []
        for spec in versions:
            version = LegalVersion(instrument_id=instrument.id, created_by=user.id, **spec)
            session.add(version)
            rows.append(version)
        await session.flush()
        return instrument.id, [row.id for row in rows]


async def test_recompute_lands_the_transition_and_writes_audit(make_user, session_factory):
    user = await make_user("knowledge_admin")
    instrument_id, (version_id,) = await _instrument(
        session_factory,
        user,
        [
            {
                "version_label": "2026年修订",
                "legal_status": "not_yet_effective",
                "promulgated_on": date(2026, 4, 30),
                "effective_from": date(2026, 11, 1),
                "review_status": "pending",
            }
        ],
    )

    async with session_factory() as session, session.begin():
        report = await recompute.recompute_statuses(session, _principal(user), today=TODAY)

    assert [(item[0], item[3]) for item in report.became_effective if item[0] == version_id] == [
        (version_id, date(2026, 11, 1))
    ]
    assert version_id not in [item[0] for item in report.repealed]

    async with session_factory() as session:
        version = await session.get(LegalVersion, version_id)
        assert version.legal_status == "effective"
        # ⚠️ 审核状态是**数据质量标记**，与时间无关——重算不该替人做确认
        assert version.review_status == "pending"
        event = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.resource_id == version_id,
                AuditEvent.action == "legal_version.became_effective",
            )
        )
        outbox = await session.scalar(
            select(OutboxEvent).where(OutboxEvent.event_type == "legal_version.became_effective")
        )
    assert event is not None and event.payload["effective_from"] == "2026-11-01"
    assert outbox is not None, "审计与 Outbox 应当一起写（设计 §12.1）"
    assert instrument_id  # 用掉变量


async def test_recompute_repeals_the_older_version(make_user, session_factory):
    user = await make_user("knowledge_admin")
    _, (old_id, new_id) = await _instrument(
        session_factory,
        user,
        [
            {
                "version_label": "2018年修正",
                "legal_status": "effective",
                "promulgated_on": date(2018, 12, 29),
                "effective_from": date(2019, 1, 1),
            },
            {
                "version_label": "2026年修订",
                "legal_status": "not_yet_effective",
                "promulgated_on": date(2026, 4, 30),
                "effective_from": date(2026, 11, 1),
            },
        ],
    )

    async with session_factory() as session, session.begin():
        report = await recompute.recompute_statuses(session, _principal(user), today=TODAY)

    assert [item[0] for item in report.repealed] == [old_id]
    async with session_factory() as session:
        assert (await session.get(LegalVersion, old_id)).legal_status == "repealed"
        assert (await session.get(LegalVersion, new_id)).legal_status == "effective"


async def test_dry_run_changes_nothing(make_user, session_factory):
    user = await make_user("knowledge_admin")
    _, (version_id,) = await _instrument(
        session_factory,
        user,
        [
            {
                "version_label": "2026年修订",
                "legal_status": "not_yet_effective",
                "promulgated_on": date(2026, 4, 30),
                "effective_from": date(2026, 11, 1),
            }
        ],
    )

    async with session_factory() as session, session.begin():
        report = await recompute.recompute_statuses(
            session, _principal(user), today=TODAY, dry_run=True
        )

    assert report.dry_run is True
    assert [item[0] for item in report.became_effective] == [version_id], "预演也要报告会改什么"
    async with session_factory() as session:
        assert (await session.get(LegalVersion, version_id)).legal_status == "not_yet_effective"


async def test_rerunning_is_a_no_op(make_user, session_factory):
    """幂等：第二次跑不该再报「有版本到期」——否则每次导入都刷一串假事件。"""
    user = await make_user("knowledge_admin")
    _, (version_id,) = await _instrument(
        session_factory,
        user,
        [
            {
                "version_label": "2026年修订",
                "legal_status": "not_yet_effective",
                "promulgated_on": date(2026, 4, 30),
                "effective_from": date(2026, 11, 1),
            }
        ],
    )

    async with session_factory() as session, session.begin():
        first = await recompute.recompute_statuses(session, _principal(user), today=TODAY)
    async with session_factory() as session, session.begin():
        second = await recompute.recompute_statuses(session, _principal(user), today=TODAY)

    assert version_id in [item[0] for item in first.became_effective]
    assert version_id not in [item[0] for item in second.became_effective], "第二次不该再报一遍"
    async with session_factory() as session:
        assert (await session.get(LegalVersion, version_id)).legal_status == "effective"


async def test_a_version_that_is_not_due_yet_is_left_alone(make_user, session_factory):
    """还没到施行日期的版本不该被提前算成有效——否则「未生效」这条屏蔽就废了。"""
    user = await make_user("knowledge_admin")
    _, (version_id,) = await _instrument(
        session_factory,
        user,
        [
            {
                "version_label": "2027年",
                "legal_status": "not_yet_effective",
                "promulgated_on": date(2026, 6, 26),
                "effective_from": date(2027, 1, 1),
            }
        ],
    )

    async with session_factory() as session, session.begin():
        report = await recompute.recompute_statuses(session, _principal(user), today=TODAY)

    assert version_id not in [item[0] for item in report.became_effective]
    async with session_factory() as session:
        assert (await session.get(LegalVersion, version_id)).legal_status == "not_yet_effective"
