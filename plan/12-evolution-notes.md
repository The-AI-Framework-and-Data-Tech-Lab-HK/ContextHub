# 12 — 架构演进备忘

MVP 阶段保持 PG-only 的简洁性，通过接口抽象为以下两个方向预留升级路径。

---

## A. 大文本存储：PG → 对象存储

### 现状

MVP 阶段所有内容（L0/L1/L2）存 PG TEXT 列，TOAST 自动处理。数据湖表的 L2 已拆为结构化子表（`table_metadata`、`lineage` 等），单字段不大。

### 何时需要升级

- 出现 MB 级别的长文档（如完整技术文档、大型 DDL）导致 TOAST 读写延迟明显
- PG VACUUM 因大量 dead tuples（频繁更新大 TEXT 列）产生性能问题
- 存储成本显著高于对象存储方案

### 预留接口

在 ContextStore 层抽象 L2 内容存储后端：

```python
class ContentBackend(ABC):
    """L2 内容存储后端，MVP 用 PG，后续可切换到对象存储"""
    async def read(self, uri: str) -> str: ...
    async def write(self, uri: str, content: str) -> None: ...

class PGContentBackend(ContentBackend):
    """直接读写 contexts.l2_content 列"""
    ...

class S3ContentBackend(ContentBackend):
    """PG 存 s3_key 引用，内容存 S3/MinIO
    注意：跨 PG 和 S3 的写入不在同一个事务中，需要处理不一致（如写 PG 成功但 S3 失败）。
    可选方案：先写 S3 → 再写 PG（失败时 S3 有孤儿对象但不影响一致性，定期清理即可）。
    """
    ...
```

### 升级时的注意事项

- 事务一致性降级：PG 元数据和 S3 内容不再原子更新，需要补偿机制
- 读取路径多一跳：PG 查 key → S3 读内容
- 迁移策略：可按 context_type 逐步迁移（先迁长文档 resources，再迁其他）

---

## B. 事件传播：LISTEN/NOTIFY → 消息队列

### 现状

MVP 阶段用 PG LISTEN/NOTIFY 做事件通知，`change_events` 表 + `processed` 字段做 outbox 补偿。

### LISTEN/NOTIFY 的已知限制

- 无持久化：传播引擎断连期间的 NOTIFY 消息丢失（靠 outbox 补偿）
- payload 限制 8000 bytes（当前只传 URI，够用）
- 无 ACK / 重试 / 死信队列
- 单 listener 模式，无法做消费者组负载均衡

### 何时需要升级

- 传播引擎需要多实例部署（高可用或负载均衡）
- 事件量大到 outbox 扫描 `WHERE NOT processed` 成为瓶颈
- 需要事件回放、死信队列等高级特性

### 预留接口

传播引擎的事件消费已经是异步迭代器模式，替换时改动很小：

```python
class EventConsumer(ABC):
    """事件消费接口"""
    async def start(self) -> None: ...
    async def events(self) -> AsyncIterator[ChangeEvent]: ...
    async def ack(self, event_id: str) -> None: ...

class PGNotifyConsumer(EventConsumer):
    """MVP：PG LISTEN/NOTIFY + outbox 补偿"""
    async def start(self):
        await self.pg.execute("LISTEN context_changed")

    async def events(self):
        async for notification in self.pg.notifications():
            # 从 change_events 表读取完整事件
            event = await self.pg.fetchrow(
                "SELECT * FROM change_events WHERE source_uri = $1 AND NOT processed LIMIT 1",
                notification.payload)
            if event:
                yield event

    async def ack(self, event_id: str):
        await self.pg.execute("UPDATE change_events SET processed = TRUE WHERE event_id = $1", event_id)

class RedisStreamConsumer(EventConsumer):
    """未来：Redis Streams，支持消费者组、ACK、死信队列
    change_events 表仍然保留作为事件持久化层（source of truth），
    Redis Streams 作为通知加速层，change_events 表仍为 source of truth。
    """
    ...
```

### 升级时的注意事项

- `change_events` 表保留，作为事件的 source of truth
- 消息队列作为通知加速层，不替代 PG 存储
- 需要处理 exactly-once 语义（outbox + 消费者幂等）
- 候选技术：Redis Streams（轻量）、NATS JetStream（云原生）、Kafka（重量级，大概率不需要）
