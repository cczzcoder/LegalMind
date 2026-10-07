"""短期内容缓存：待审队列「看得到当时答了什么」的唯一来源（设计 §9.3 第三层、§21）。

**为什么需要**：运行记录里**只有元数据与哈希，没有正文**（§21「客户数据不落库」），所以复核人
打开待审队列**看不到当时答了什么**。给复核人重跑一次也不行——生成有随机性（哪怕温度 0，
证据或版本变了也不是同一份），重跑出来的不是「当时那份」，审核就失去意义。

**按「短期缓存 + 强制脱敏 + 过期即焚」办**：

- 缓存的是**跑过脱敏的回答文本**（`redaction.redact`），**不缓存模型的原始输出**；
- **TTL 到期自动清理**（默认 24 小时）——数据最小化，不进主库、不进备份；
- Redis 只作 **TTL 键值存储，不作队列**：队列用自研 worker（`FOR UPDATE SKIP LOCKED` 原子领取 +
  租约），**不引入 Celery**。⚠️ 设计 §14.1 原先把「Redis + Celery」一起列在「暂不启动」里；
  V1.26 **只取其中的 Redis**，且只用于这一件事。

**未配置即禁用**：`CACHE_URL` 为空时缓存不可用。此时**异步问答拒绝提交**（提交方靠缓存取结果，
没有缓存就没有结果可交付），而**同步问答照常**——它把结果直接返回给调用方，不依赖缓存。
**降级要看得见**：`available()` 为假时，待审队列会如实显示「内容不可用」，而不是静默留空。
"""

from dataclasses import dataclass

from app.core.config import Settings

#: 键前缀。带上它便于在 Redis 里一眼分辨哪些键是本系统的、也便于按前缀批量清理。
KEY_PREFIX = "legalmind:answer:"


def cache_key(run_id) -> str:
    return f"{KEY_PREFIX}{run_id}"


@dataclass
class AnswerCache:
    """短期内容缓存。`client` 为 None 表示**未配置**（禁用）。"""

    client: object | None = None
    ttl_seconds: int = 86400

    @property
    def available(self) -> bool:
        return self.client is not None

    async def put(self, key: str, text: str) -> bool:
        """写入并设置 TTL。**到期由 Redis 自己清理**，不需要清理任务。"""
        if self.client is None:
            return False
        await self.client.set(key, text, ex=self.ttl_seconds)
        return True

    async def get(self, key: str) -> str | None:
        if self.client is None:
            return None
        value = await self.client.get(key)
        return value.decode("utf-8") if isinstance(value, bytes) else value

    async def delete(self, key: str) -> None:
        """提前销毁（复核完成、或运行失败时）。**不留副本**。"""
        if self.client is not None:
            await self.client.delete(key)

    async def close(self) -> None:
        if self.client is not None:
            await self.client.aclose()


def from_settings(settings: Settings) -> AnswerCache:
    """按配置构造。`CACHE_URL` 为空 → 禁用（返回一个 `available=False` 的缓存）。"""
    url = (settings.cache_url or "").strip()
    if not url:
        return AnswerCache()
    # 惰性导入：没配缓存的环境不必装 redis 客户端
    from redis.asyncio import Redis

    return AnswerCache(
        client=Redis.from_url(url, encoding="utf-8", decode_responses=False),
        ttl_seconds=settings.answer_cache_ttl_seconds,
    )
