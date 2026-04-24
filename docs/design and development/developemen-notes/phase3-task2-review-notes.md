# Phase 3 Task 2 (FeedbackService) — Review Notes

本文档记录 Task 2 code review 中识别出的设计决策和潜在改进点，供未来优化时参考。

---

## 1. FOR UPDATE vs Advisory Lock 序列化策略

### 现状

`record_feedback()` 使用了**双重序列化**：

1. `SELECT ... FROM contexts WHERE uri = $1 AND status != 'deleted' FOR UPDATE` — 锁住 contexts 行
2. `pg_advisory_xact_lock(hash(account_id, context_id, retrieval_id, actor))` — 按幂等键细粒度锁

### 分析

- **FOR UPDATE** 的粒度是 context 行级。所有对同一 context 的并发 feedback（即使 retrieval_id 不同）都被完全串行化。
- **Advisory lock** 的粒度是幂等键级。只有同一 (context_id, retrieval_id, actor, account_id) 才互相阻塞。
- 当两者同时存在时，FOR UPDATE 的粗粒度完全覆盖了 advisory lock 的细粒度，advisory lock 退化为冗余。

### FOR UPDATE 的额外价值

FOR UPDATE 解决了一个 spec 未显式覆盖的竞态：在"检查 context 存在 → 写入 feedback → 更新计数"的窗口期内，另一个事务可能将 context 标记为 deleted。FOR UPDATE 阻止了这种情况，保证了 feedback 不会指向一个刚被删除的 context。

### 未来优化方向（建议 Phase 5 Production Hardening 时评估）

如果 feedback 并发量上升：

- **方案 A**：移除 FOR UPDATE，只保留 advisory lock。接受极小概率的 "feedback 指向已 deleted context" 情况，后续 sweep 可清理孤儿 feedback。
- **方案 B**：保留 FOR UPDATE 但用 `SKIP LOCKED` + 重试模式替代阻塞等待。
- **方案 C**：将计数更新改为异步聚合（定期从 context_feedback 重算），彻底消除 contexts 行上的写竞争。

### 当前结论

Phase 3 低并发场景下保留双重锁是安全且保守的。不阻塞后续 Task。

---

## 2. `_row_to_feedback()` 对 DB 行结构的隐含依赖

### 现状

```python
def _row_to_feedback(self, row) -> ContextFeedback:
    return ContextFeedback(**dict(row))
```

这依赖 `RETURNING *` 返回的列名与 `ContextFeedback` Pydantic 模型的字段名完全匹配。

### 风险评估

- **当前安全**：DB 列名和模型字段名一致，Pydantic v2 默认忽略多余字段（不会因 DB 加列而报错）。
- **潜在风险**：如果 DB 列名被 rename 或模型字段被重命名，不会有编译期错误，只有运行时 ValidationError。
- **对比**：仓库内其他 service（如 ContextStore）也使用类似模式，这是项目既有风格。

### 建议

暂不修改（保持与项目风格一致）。如果后续 DB schema 或模型发生重命名，应优先考虑引入显式映射层或 row → dict 的字段白名单。
