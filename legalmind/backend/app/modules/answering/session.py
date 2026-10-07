"""多轮追问的会话（设计 §9.6、需求 FR-16）。

**它解决的问题**：让「那如果是这样呢？」这类追问继承上文，用户不必重复已经说过的条件。

**三条边界不可放开**（§9.6 原话，这里只写实现怎么落）：

1. **上文只用来把追问补全成自足的问题；历史「答案」绝不进提示词。**
   补全后的「问题」在链路里**就是** `question`（检索与提示都用它）——问题的形状是用户输入、
   不构成依据；而历史**答案**含结论，进了提示词就会变成模型的「依据」，可它既没经过本轮的
   授权复核、也没绑定条款版本（§5.3），而 §9.3「每条主张必须挂本次证据编号」的前提也会失效。
2. **会话历史是客户数据 → 只进短期缓存、不落主库。**
   追问里必然出现当事人的具体情形（「我们公司没给我缴」），那就是 §21.1 禁止存放的主体数据。
   缓存里只放**脱敏后的问题**与**证据引用**，**不放答案正文**（正文另有 `answer:{run_id}`，
   存两份副本早晚不一致）；TTL 与答案缓存同值、到期即焚。
3. **会话只是分组**：每条追问产生**独立的 run**，各自过 §9.3 核验与 §20.2 声明；
   **会话整体不构成正式输出**，界面不得把整段会话当一份结论展示。

**越权按不存在处理**：会话属于提交者本人。键存在但 `user_id` 不是当前 principal 时，
一律当作「没有这个会话」返回空——**不泄露「存在但无权」**。

**降级**：`CACHE_URL` 为空（缓存禁用）时多轮不可用，**单轮问答照常**——不因为会话功能缺失
挡住主链路（与 §9.4 异步问答「缓存不可用则拒绝提交」同一口径，但这里不能拒绝，因为单轮是主链路）。
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.security import Principal

#: 会话键前缀。
KEY_PREFIX = "legalmind:conversation:"

#: 缓存里保留多少轮。**只是留个上下文痕迹，不参与回答**（§9.6 明确不做会话列表/管理界面）。
MAX_TURNS_KEPT = 5

#: 实际拼进查询的上文轮数。**只取最近 1 轮是有意的**——拼接越长，检索越容易被上文里的
#: 无关实体带偏（§9.6 的实现口径）。这与「不把历史答案喂给模型」是同一个保守取向。
CONTEXT_TURNS = 1


def conversation_key(session_id) -> str:
    return f"{KEY_PREFIX}{session_id}"


@dataclass(frozen=True)
class Turn:
    """一轮问答的**可复用痕迹**：脱敏后的问题 + 它引用了哪些条款版本。"""

    question: str
    provision_version_ids: tuple[str, ...] = ()
    run_id: str | None = None
    at: str | None = None


def compose_query(turns: list[Turn], followup: str) -> str:
    """把追问补全成**自足的问题**。没有上文时原样返回。

    做法就是拼接——**不做指代消解**（§9.6 明确不做：「它」「该条」指哪一条，规则做不可靠，
    让模型推断又违反边界 1）。拼接的好处是**失败方向安全**：拼多了只是检索范围略宽，
    而检索本身还有 §8.2 的级联与授权复核兜底；拼错了也不会造出不存在的依据。
    """
    if not turns:
        return followup
    recent = turns[-CONTEXT_TURNS:]
    parts = [item.question.strip() for item in recent if item.question.strip()]
    parts.append(followup.strip())
    return " ".join(part for part in parts if part)


def _load(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


async def read_turns(cache, session_id, principal: Principal) -> list[Turn]:
    """读会话历史。**越权或不存在都返回空列表**（调用方区分不了，也不该区分）。"""
    if cache is None or not cache.available:
        return []
    data = _load(await cache.get(conversation_key(session_id)))
    if data is None:
        return []
    # 越权：按「没有这个会话」处理，**不要返回「无权访问」**——那等于确认了会话存在
    if data.get("user_id") != str(principal.user_id):
        return []
    turns = data.get("turns")
    if not isinstance(turns, list):
        return []
    result: list[Turn] = []
    for item in turns:
        if not isinstance(item, dict):
            continue
        question = item.get("q")
        if not isinstance(question, str) or not question.strip():
            continue
        ids = item.get("v")
        result.append(
            Turn(
                question=question,
                provision_version_ids=tuple(ids) if isinstance(ids, list) else (),
                run_id=item.get("run"),
                at=item.get("at"),
            )
        )
    return result


async def record_turn(
    cache,
    session_id,
    principal: Principal,
    question: str,
    answer,
    run_id=None,
) -> bool:
    """把这一轮记进会话。**写的是脱敏后的问题与证据引用，不是答案正文**（边界 2）。

    返回是否写入成功（缓存不可用时为 False——**不影响本次回答**，多轮只是少了上下文）。
    """
    if cache is None or not cache.available:
        return False

    from app.modules.redaction.service import redact

    redacted_question, _ = redact(question)
    # 复用同一个键的读—改—写。⚠️ 这不是原子操作：**并发追问可能丢一轮**。
    # 这是**有意接受的**——会话只是上下文增强，丢一轮最多让下一次追问少一点上下文，
    # 不会造成错误结论；为它引入分布式锁不值当（与「没有测量支撑的组件不装」同一口径）。
    existing = await read_turns(cache, session_id, principal)
    turns = list(existing)
    turns.append(
        Turn(
            question=redacted_question,
            provision_version_ids=tuple(
                str(item.provision_version_id) for item in getattr(answer, "citations", ())
            ),
            run_id=str(run_id) if run_id is not None else None,
            at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
    )
    payload = {
        "user_id": str(principal.user_id),
        "turns": [
            {
                "q": item.question,
                "v": list(item.provision_version_ids),
                "run": item.run_id,
                "at": item.at,
            }
            for item in turns[-MAX_TURNS_KEPT:]
        ],
    }
    # `cache.put` 自带 TTL，所以「过期即焚」由 Redis 保证，不需要清理任务
    return await cache.put(conversation_key(session_id), json.dumps(payload, ensure_ascii=False))
