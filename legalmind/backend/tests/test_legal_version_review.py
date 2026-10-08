"""法律版本的人工复核（`app/modules/legal_corpus/review.py`；需求第 3 节）；需要 TEST_DATABASE_URL。

**为什么单独钉**：`legal_versions.review_status` 原先只有**自动**写入——低置信度的版本被置成
`pending` 之后，**没有任何路径能改掉它**（CLI/API/界面都没有，只能改 SQL）。这里钉三件事：

1. 复核**只改审核状态**，绝不碰 `legal_status`——后者是法律事实，不是审核意见；
2. 复核**允许纠正**（`approved` 还能改 `rejected`）——与问答运行「复核一次就定」刻意不同，
   因为 `review_status` 是**当前状态**而不是历史记录；
3. 每次复核**写审计**（谁、什么时候、从什么改到什么、备注）。
"""

from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.security import Principal
from app.models import AuditEvent, LegalInstrument, LegalVersion
from app.modules.legal_corpus import review

pytestmark = pytest.mark.anyio


def _principal(user) -> Principal:
    return Principal(organization_id=user.organization_id, user_id=user.id, roles=frozenset())


async def _land_version(session_factory, user, *, review_status="pending", legal_status="unknown"):
    """直接落一条版本记录。

    **不走解析链路**：这条测试量的是「复核怎么改状态」，而落库时的置信度是启发式判的
    （要凑出 `pending` 得先构造一份元数据不完整的文本），绕一圈只会让失败原因变模糊。
    解析链路自己的测试在 `test_legal_version_integration.py`。
    """
    async with session_factory() as session:
        async with session.begin():
            instrument = LegalInstrument(
                title=f"示例测试法（{uuid4().hex[:8]}）",
                jurisdiction="中国",
                issuing_body="全国人民代表大会常务委员会",
                instrument_type="law",
                created_by=user.id,
            )
            session.add(instrument)
            await session.flush()
            version = LegalVersion(
                instrument_id=instrument.id,
                version_label="2026年修订",
                legal_status=legal_status,
                review_status=review_status,
                created_by=user.id,
            )
            session.add(version)
        return instrument.id, version.id


async def test_review_approves_and_records_who_did_it(make_user, session_factory):
    user = await make_user("legal_reviewer")
    _, version_id = await _land_version(session_factory, user)

    async with session_factory() as session, session.begin():
        version = await review.review_version(
            session,
            _principal(user),
            version_id,
            decision="approved",
            note="公布信息完整，人工确认。",
        )
        assert version.review_status == "approved"

    async with session_factory() as session:
        event = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.resource_id == version_id,
                AuditEvent.action == "legal_version.reviewed",
            )
        )

    assert event is not None, "复核必须写审计"
    assert event.actor_id == user.id, "审计里要能看出是谁复核的"
    assert event.payload["from"] == "pending"
    assert event.payload["to"] == "approved"
    assert event.payload["note"] == "公布信息完整，人工确认。"


async def test_review_never_touches_the_legal_status(make_user, session_factory):
    """⚠️ 这条是**安全不变量**：复核是数据质量意见，不是法律效力判断。

    把「已废止」点成「现行有效」正是本系统要防的事（§8.3 靠效力状态屏蔽失效版本）。
    """
    user = await make_user("legal_reviewer")
    _, version_id = await _land_version(
        session_factory, user, review_status="pending", legal_status="repealed"
    )

    async with session_factory() as session, session.begin():
        version = await review.review_version(
            session, _principal(user), version_id, decision="approved"
        )
        assert version.legal_status == "repealed", "复核不得改动效力状态"

    async with session_factory() as session:
        stored = await session.get(LegalVersion, version_id)
        assert stored.legal_status == "repealed"
        assert stored.review_status == "approved"


async def test_review_can_be_corrected(make_user, session_factory):
    """审错了要能纠正——否则只是把「只能改 SQL」换个地方再来一遍。"""
    user = await make_user("legal_reviewer")
    _, version_id = await _land_version(session_factory, user)

    async with session_factory() as session, session.begin():
        await review.review_version(
            session, _principal(user), version_id, decision="approved", note="先确认"
        )
    async with session_factory() as session, session.begin():
        version = await review.review_version(
            session,
            _principal(user),
            version_id,
            decision="rejected",
            note="复核后发现标题取自文件名",
        )
        assert version.review_status == "rejected"

    async with session_factory() as session:
        events = (
            await session.scalars(
                select(AuditEvent)
                .where(AuditEvent.resource_id == version_id)
                .order_by(AuditEvent.created_at)
            )
        ).all()
    assert [event.payload["to"] for event in events] == ["approved", "rejected"], "两次复核都要留痕"


async def test_repeating_the_same_decision_is_refused(make_user, session_factory):
    """重复同一结论是空转，应该报错而不是堆一串什么也没改变的审计。"""
    user = await make_user("legal_reviewer")
    _, version_id = await _land_version(session_factory, user)

    async with session_factory() as session, session.begin():
        await review.review_version(session, _principal(user), version_id, decision="approved")

    async with session_factory() as session, session.begin():
        with pytest.raises(ValueError, match="已经是"):
            await review.review_version(session, _principal(user), version_id, decision="approved")


async def test_pending_is_not_a_decision(make_user, session_factory):
    """`pending` 是「机器没把握」，不是人能做出的结论——要退回待审就撤回原件重新导入。"""
    user = await make_user("legal_reviewer")
    _, version_id = await _land_version(session_factory, user, review_status="approved")

    async with session_factory() as session, session.begin():
        with pytest.raises(ValueError, match="不认识的复核结论"):
            await review.review_version(session, _principal(user), version_id, decision="pending")


async def test_unknown_version_is_a_lookup_error(make_user, session_factory):
    user = await make_user("legal_reviewer")
    async with session_factory() as session, session.begin():
        with pytest.raises(LookupError, match="不存在"):
            await review.review_version(session, _principal(user), uuid4(), decision="approved")


async def test_pending_list_only_shows_pending_and_carries_the_title(make_user, session_factory):
    user = await make_user("legal_reviewer")
    _instrument_id, pending_id = await _land_version(session_factory, user)
    _, done_id = await _land_version(session_factory, user, review_status="approved")

    async with session_factory() as session, session.begin():
        await review.review_version(session, _principal(user), done_id, decision="rejected")

    async with session_factory() as session:
        rows = await review.pending_versions(session, limit=100)

    ids = [version.id for version, _title, _filename in rows]
    assert pending_id in ids
    assert done_id not in ids, "已复核的不该再出现在待审队列里"

    matched = [title for version, title, _f in rows if version.id == pending_id]
    assert matched, "列表要带上法律名称——只给 id 操作者认不出是哪部法律"
    assert matched[0].startswith("示例测试法")

    # 原件是外连接：允许先登记版本、后导入文件，此时文件名是 None 而不是把整行漏掉
    filenames = [filename for version, _t, filename in rows if version.id == pending_id]
    assert filenames == [None]


async def test_versions_without_an_artifact_still_appear(make_user, session_factory):
    """外连接的回归：`artifact_id` 可空（先登记版本、后导入文件），不能因为没原件就查不出来。"""
    user = await make_user("legal_reviewer")
    _, version_id = await _land_version(session_factory, user)

    async with session_factory() as session:
        rows = await review.pending_versions(session, limit=100)
    assert version_id in [version.id for version, _t, _f in rows]

    async with session_factory() as session:
        stored = await session.get(LegalVersion, version_id)
        assert stored.artifact_id is None, "这条测试的前提是版本没有挂原件"
