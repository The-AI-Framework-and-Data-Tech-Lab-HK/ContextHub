# ContextHub 治理正确性 Benchmark 报告说明

本文档说明 `scripts/benchmark_workflow.py` 的用途、如何运行，以及如何阅读终端输出（含每行每列含义）。

---

## 1. 测试是什么

该脚本对 ContextHub **治理层**做端到端 HTTP 验证（非 LLM 评测），覆盖：

| Suite | 名称 | 检查项数 | 主要验证点 |
|-------|------|----------|------------|
| 1 | Isolation Correctness | 13 | 跨 Agent 私有隔离、搜索不泄漏、选择性晋升、批量隔离 |
| 2 | Promotion & Sharing | 12 | 双向晋升、共享空间列表、`read/stat`、统一搜索 |
| 3 | Version Resolution | 10 | Skill 发布、pinned 订阅、breaking 与 advisory、版本列表、PATCH 不可变 |
| 4 | Propagation & Convergence | 10 | 变更传播收敛时延、catalog sync、sql-context、幂等 sync |
| 5 | LLM-Native Tools API | 12 | `ls`/`read`/`stat`/`search`、404、技能版本读取 |

**合计**：约 **57** 条检查；每条为通过/失败二元结果。脚本退出码：`0` 表示全部通过，`1` 表示至少一条失败。

---

## 2. 前置条件

- PostgreSQL 已启动，且已执行 `alembic upgrade head`
- ContextHub Server 已启动，默认 `http://localhost:8000`
- 与 `demo_e2e.py` 相同：`query-agent` 需为 `engineering` 团队成员（脚本会尝试用 `asyncpg` 插入 `team_memberships`，失败时仅打印警告；若 I-10 等共享相关用例失败，请按 `mvp-verification-plan.md` 手动补 membership）

---

## 3. 如何运行

在仓库根目录（`ContextHub/`）下：

```bash
# 激活虚拟环境后
python scripts/benchmark_workflow.py
```

**常用参数**：

| 命令 | 含义 |
|------|------|
| `python scripts/benchmark_workflow.py` | 运行全部 5 个 Suite |
| `python scripts/benchmark_workflow.py --suite 1` | 只运行 Suite 1（隔离） |
| `python scripts/benchmark_workflow.py --suite 2,4` | 逗号分隔，运行 Suite 2 与 4 |

**环境变量**：脚本内写死 `BASE_URL=http://localhost:8000`、`API_KEY=changeme`、`X-Account-Id=acme`。若你本地不同，需改 `benchmark_workflow.py` 顶部常量或自行封装。

**每次运行**会生成 `Run ID`（Unix 时间戳），用于区分 URI/标签，降低与历史数据冲突的概率。

---

## 4. 输出结构总览

典型输出自上而下为：

1. `Server healthy. Run ID: …`
2. 每个 **Suite** 一块报告（Suite 名、通过率、逐条检查）
3. **LATENCY PROFILE** 表（按操作类型的延迟分布）
4. **GOVERNANCE CORRECTNESS BENCHMARK** 总览（各维度通过率 + Overall %）

下面分节说明每一部分的「行 / 列」含义。

---

## 5. 每个 Suite 块：如何读

示例（示意）：

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Suite 1 · Isolation Correctness
  13/13 = 100%
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ✓ I-01 qa stores 'promo rules'  (45 ms)  —
  ✗ I-10 [POS] aa sees promoted promo rules  (12 ms)  — promoted promo rules NOT visible to aa
```

| 位置 | 含义 |
|------|------|
| 第一行 `Suite N · …` | 第 N 套测试的名称 |
| 第二行 `x/y = z%` | **x** 通过条数，**y** 总条数，**z** 该 Suite 通过率 |
| 以下各行 | 单条检查：`✓` 通过，`✗` 失败 |
| 检查名（如 `I-01 …`） | 编号 + 简短描述；`[POS]` 表示预期成功、`[NEG]` 表示预期不应发生泄漏等 |
| `(NN ms)` | 该检查**整段协程**的墙钟时间（含内部多次 HTTP 调用），不是单请求 RTT |
| `—` 后的文字 | **detail**：通过时多为补充说明；失败时为失败原因 |

> 注意：单条检查的毫秒数**不等于**下方 LATENCY PROFILE 里的单次 API 耗时；后者只统计 `tpost`/`tget`/`tpatch` 包装的请求。

---

## 6. LATENCY PROFILE 表：每列含义

示例（示意）：

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  LATENCY PROFILE  (per-operation, ms)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Operation                   n      p50      p95      p99
  ────────────────────────────────────────────────────────
  catalog_sync                2        85      120      120
  memory_list                 8        28       45       45
  memory_store               14        42       68       72
  promote                     4        38       55       55
  propagation_convergence     1       156      156      156
  read                       25        12       35       40
  search                      5        85      120      120
  skill_publish               6        52       95       95
  ...
```

| 列名 | 含义 |
|------|------|
| **Operation** | 脚本内部对一类 HTTP 调用的**标签**（见下节「Operation → API」）。同一标签下可能对应同一端点、不同 body。 |
| **n** | 该 Operation **被计时的次数**（样本数）。一次 `tpost`/`tget`/`tpatch` 记 1 次；循环内每次成功记录 `propagation_convergence` 也算 1 次。 |
| **p50** | **中位数**延迟（毫秒）：有一半样本 ≤ 该值，有一半 ≥ 该值。 |
| **p95** | **第 95 百分位**（毫秒）：约 95% 的请求耗时不超过该值（尾部延迟上界参考）。 |
| **p99** | **第 99 百分位**（毫秒）：约 99% 的请求耗时不超过该值（更极端尾部）。 |

**单位**：全部为 **毫秒（ms）**。

**计算方式**：对某一 Operation 的所有样本排序后，用线性插值在排序数组上取对应百分位（与常见「延迟报表」一致）。当 **n = 1** 时，p50 / p95 / p99 数值相同，均为那一次观测值。

**与「性能测试」的关系**：该表反映**当前机器、当前数据库负载、当前网络回环**下的粗粒度分布，用于回归对比或发现异常尖刺；**不是**对外承诺的 SLA，也不替代压测。

**未计入表的请求**：少数检查（例如 Suite 4 中轮询 `read` 直到出现 `advisory`）使用**未经过** `tpost`/`tget` 的原始 `httpx` 调用；这些请求的耗时**不会**出现在 `read` 行，但 `propagation_convergence` 会单独记录「从进入轮询到首次成功」的间隔。

---

## 7. Operation 标签与 API 对应（便于对照日志）

| Operation 标签 | 对应 HTTP（摘要） |
|----------------|---------------------|
| `memory_store` | `POST /api/v1/memories` |
| `memory_list` | `GET /api/v1/memories` |
| `promote` | `POST /api/v1/memories/promote` |
| `search` | `POST /api/v1/search` |
| `ls` | `POST /api/v1/tools/ls` |
| `read` | `POST /api/v1/tools/read` |
| `stat` | `POST /api/v1/tools/stat` |
| `context_create` | `POST /api/v1/contexts` |
| `skill_publish` | `POST /api/v1/skills/versions` |
| `skill_subscribe` | `POST /api/v1/skills/subscribe` |
| `skill_versions` | `GET /api/v1/skills/{uri}/versions` |
| `context_patch` | `PATCH /api/v1/contexts/{uri}` |
| `catalog_sync` | `POST /api/v1/datalake/sync` |
| `datalake_list` | `GET /api/v1/datalake/{catalog}/{database}` |
| `sql_context` | `POST /api/v1/search/sql-context` |
| `propagation_convergence` | 从发布 breaking 版本到 **首次** `read` 返回 `advisory` 的间隔（单独计时，不一定经过 `tpost`） |

---

## 8. 文末总览块：如何读

示例（示意）：

```
══════════════════════════════════════════════════════════════════
  GOVERNANCE CORRECTNESS BENCHMARK   (run 1743523200)
  57/57 checks passed, 0 failed
──────────────────────────────────────────────────────────────────
  ✓ isolation        13/13 = 100%
  ✓ sharing          12/12 = 100%
  ✓ versioning       10/10 = 100%
  ✓ propagation      10/10 = 100%
  ✓ tools            12/12 = 100%
──────────────────────────────────────────────────────────────────
  Overall governance correctness: 100.0%
══════════════════════════════════════════════════════════════════
```

| 字段 | 含义 |
|------|------|
| `run 1743523200` | 本次运行的 Run ID（与开头打印一致） |
| `57/57 checks passed` | 全部 Suite 合计：通过数 / 总数 |
| `isolation` / `sharing` / … | 各维度的**短标签**与通过率 |
| `Overall governance correctness` | 全部检查中通过条数占比（%） |

---

## 9. 建议归档方式（用于 MVP 材料）

1. 运行：`python scripts/benchmark_workflow.py > benchmark-run.txt 2>&1`（或复制终端全文）。
2. 将 `benchmark-run.txt` 与本文档一并放入版本管理或附件。
3. 在 `mvp-verification-plan.md` 的产出清单中引用：**治理正确性 benchmark 原始输出 + 本说明文档**。

---

## 10. 与自动化测试（pytest）的关系

- **pytest**（`CONTEXTHUB_INTEGRATION=1`）：组件/集成级、可 gate CI。
- **本 benchmark**：面向「治理场景」的**长流程、多 API 组合**，并输出**分类通过率 + 延迟表**，便于对外说明与人工回归。

二者互补；本脚本**不替代** pytest。
