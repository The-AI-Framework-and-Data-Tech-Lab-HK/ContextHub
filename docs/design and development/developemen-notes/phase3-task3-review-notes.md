# Phase 3 Task 3 (LifecycleService + Scheduler) — Review Notes

本文档记录 Task 3 code review 中识别出的后续优化点，供未来迭代时参考。

---

## 1. Skill 读路径代码重复

### 现状

`api/routers/contexts.py` 的 `read_context()` 和 `api/routers/tools.py` 的 `tool_read()` 中，skill read + stale recovery + conditional `last_accessed_at` 更新逻辑约 30 行几乎完全相同：

```python
if row["context_type"] == "skill":
    decision = await acl.check_read_access(db, uri, ctx)
    # ... ACL deny handling ...
    if row["status"] == "stale" and _lifecycle is not None:
        await _lifecycle.recover_from_stale(db, row["id"], ctx)
    result = await skill_svc.read_resolved(db, row["id"], ctx.agent_id, version)
    if row["status"] != "stale" or _lifecycle is None:
        await db.execute("UPDATE contexts SET last_accessed_at = NOW() WHERE uri = $1", uri)
    # ... masking + audit ...
```

### 风险

如果后续需要修改 skill 读路径的生命周期语义（例如 Task 4 检索过滤集成、或 archived skill 的特殊处理），需要同步改两处，容易遗漏。

### 建议（Phase 5 或 Task 7 时评估）

抽取一个 `async def _read_skill_with_lifecycle(...)` 共享函数，放在 `api/routers/contexts.py` 或独立 module 中，`tools.py` 调用它。当前不阻塞后续 Task。

---

## 2. `_sweep_tenant` 单事务处理整个 tenant

### 现状

`LifecycleScheduler._sweep_tenant()` 把 `ensure_default_policies` + 三段 sweep（stale / archive / delete）全部放在一个 `repo.session()` 事务中：

```python
async with self._repo.session(account_id) as db:
    await self._lifecycle.ensure_default_policies(db, ctx=ctx)
    # stale candidates → mark_stale each
    # archive candidates → mark_archived each
    # delete candidates → mark_deleted each
```

### 风险

对于 context 数量大的 tenant，这意味着一个长事务，可能：
- 长时间持有数据库连接
- 在高并发场景下增加锁竞争

### 建议（Phase 5 Production Hardening 时评估）

- **方案 A**：按 sweep 阶段分事务 — 每个阶段（stale / archive / delete）使用独立的 `repo.session()`。
- **方案 B**：批量分页 — 每 N 个 candidate（如 100）一个事务，带分页查询。
- **方案 C**：保持现状，增加 per-candidate try/catch — 单个 candidate 失败不阻塞同 tenant 其他 candidate。

当前 MVP 阶段低数据量下可接受，不阻塞后续 Task。

---

## 已修复的问题（本轮 review 中直接修复）

以下两项已在 review 中直接修复，记录于此以备追溯：

1. **测试中 `audit` 参数隐式处理**：`test_skill_resolution.py` 中直接调用 `read_context()` / `tool_read()` 的 4 处测试未显式传 `audit=None`，依赖 `isinstance(audit, AuditService)` 将 `Depends(...)` 哨兵对象过滤为 None。已修复为显式传入 `audit=None`。

2. **缺少 `mark_stale` 幂等性测试**：`test_lifecycle_service.py` 中新增 `test_mark_stale_is_idempotent`，验证对已 stale 的 context 再次调用 `mark_stale` 不会重复写入 `change_events` 或 `audit_log`，且 `stale_at` 不变。
