# memebench — 目录导航

MEME benchmark 上的 ContextHub 验证代码。当前方案：**P2 只标记过期 + 把过期原因带给回答模型**
（权威地图：`ContextHub-research-plan/research/proposal/WORKING-NOTES-p2-semantic-recompute.md`）。

根目录只放**当前方案在用的 23 个模块**。已结题的实验复现代码不进入 upstream
主分支，完整版本保存在
[S0 快照分支](https://github.com/heathersherry/ContextHub/tree/codex/s0-full-snapshot-20260922/integrations/memebench/archive)。

分类依据是机械证据，不是文件名：**活代码的 import 传递闭包 ∪ 测试引用**
（`integrations.memebench.X` 绝对路径写法）+ 与 `runs/` 产物的时间线对齐。
"测试引用"这一项是必须的 —— 只算活代码闭包会漏掉仅被测试保活的模块。

---

## 一、入口（能直接跑的）

所有命令都从**仓库根**执行，且需要 `CONTEXTHUB_INTEGRATION=1`。

| 入口 | 用途 | CLI |
|---|---|---|
| `run_full100_v3_p2.py` | **主运行器**（6206 行）：冻结 v3 P1 + 生产 durable P2，full100 | `preflight` / `smoke` / `paid-case` / `run` 四道闸门 |
| `full100_continuation.py` | 上面这个的**零 API** 续跑与迁移（源 run 只读） | `validate-continuation` / `create-continuation` |
| `run_abs_official_rejudge.py` | 闸门 1：用 MEME 官方判据离线重判已落盘的 `Abs` 答案（不生成） | `preflight` / `smoke` / `run` |
| `abs_report.py` | 任务④分层统计：三段判据**分别**报，hop2 分两层 | `abs_report.py <run_dir>` |
| `abs_cost_audit.py` | 核账：判据 `inference_calls < n_ok` 即为漏计 | `abs_cost_audit.py <run_dir>` |
| `calibration_cluster_bootstrap.py` | δ 的**按 episode 整簇** bootstrap（2026-09-03 已跑） | `--runs-dir --iters --replicates` |
| `propagation_planner_eval.py` | P2 契约标定 `calibrate_contracts`（零 DB / 零 API） | `--contract point\|cp-upper` |
| `synthetic_planner_eval.py` | 合成拓扑规划器评测（供 `test_memebench_propagation_planner_eval.py`） | `--help` |

四档正式跑的复现脚本：`runs/formal_20260902_all_four.sh`
（串行；并行会在同一个慢代理上互相拖慢约 5×）。

## 二、库（被入口 import，不单独跑）

| 模块 | 职责 |
|---|---|
| `systems.py` | 装配 ContextHub 服务（真实 OpenAI 兼容客户端，走 openlux 代理） |
| `loader.py` | 载入 MEME、按 `task_type` 抽题（**`task_type` 无默认值，缺则拒跑**） |
| `ingest.py` | Stage B/C：入库一个 case，再施加 `root_change` |
| `answer.py` | Stage E：检索 + 生成（ON/OFF 两臂检索配置严格一致） |
| `judge.py` | Stage F：`Cas` 判定 + trivial-pass 过滤 |
| `abs_judge.py` | `Abs` 三段确定性判定器（自创口径，比官方严；不动 `judge.py`） |
| `meme_official_judge.py` | MEME 官方判词逐字转写，用于平价重读 |
| `abs_official_rejudge.py` | 官方判据离线重判的实现体 |
| `planned_propagation.py` | **P2 执行器**：按冻结的图级 plan 传播（不改生产 rule/engine 的输出） |
| `chronological_policy.py` | 冻结的 P1 建图策略菜单（`registered_build_plans`）；`common.py` 与 `planned_propagation` 都要它，故留活 |
| **`common.py`** | **共享符号单一真源**：`DEFAULT_DATA`、per-case token 计量、episode dataclass、`stable_episode_split`、`clopper_pearson_upper`、`judge_j1`、`wipe_account`、`bind_root_plan`。全部从原文**逐字节**搬来，见第四节 |
| `cost.py` / `cost_interval.py` | 计费计数（answer / oracle 分桶）+ 精确/有界/无界成本口径 |
| `embedding_retry.py` | embedding 重试包装（yunwu 代理偶发 ReadTimeout） |

## 三、⚠️ 改动会使冻结产物失效的文件

`run_full100_v3_p2.py:146` 起有一张 **identity 输入清单**，跑的时候对其中每个文件做内容哈希。
清单里的文件**改一个字节**，已有 run 的 identity 校验就不通过：

```
run_full100_v3_p2  planned_propagation  common  systems
answer  judge  cost  loader  ingest  embedding_retry
```

（`common.py` 已取代原先的 `run_chronological_p1p2.py`：主运行器实际执行的
`bind_root_plan` / `wipe_account` 现在在 common 里。动态闭包 76 个文件，
memebench 部分 14 个，已验证零历史复现文件混入。）

旧 `run_chronological_p1p2.py` 另有一张**软判断**的指纹表
`SOURCE_FINGERPRINT_FILES`，它用 `path.exists()` 逐个读字节，文件缺失时不报错但会让
指纹静默改变。该旧 runner 及其完整复现闭包只保留在 S0 快照分支，不属于当前执行路径。

### ⚠️ identity 校验目前已经失效（实测，2026-09-04）

三个正式 run 全部校验不通过：

```
formal_abs_hop1_20260902_final : FAILS -> run identity input changed: propagation_planner_eval.py
formal_cas_hop1_20260902_final : FAILS （同）
formal_abs_hop2_20260902_final : FAILS （同）
```

原因是 `propagation_planner_eval.py` 在 **09-03 18:08** 被改过（WORKING-NOTES 第 1 步的标定改动），
而这批 run 是 **09-02 14:36** 冻结的。该文件虽不在明文清单里，但通过 `planned_propagation`
的传递闭包进了 bundle。`run_abs_official_rejudge.py` 里也记录了同类先例
（为发 temperature 改 `chat_client.py`），并指出这些 bundle 早已因
`model_providers.local.json`（密钥文件，本就会漂移）而无法验证。

**含义**：想恢复"可验证"状态，需要用当前代码重跑一轮冻结。在那之前，
改 identity 清单里的文件不会让情况变得更糟 —— 但也不要以为它还在保护什么。

WORKING-NOTES **待办 1** 的两处硬编码仍待你决定（我没有改）：
- `run_full100_v3_p2.py:4122` 的 `epsilon=0.1`
- **`chronological_policy.py:29` 的 `EPSILON_PROP = 0.10`** ← 本次新发现的第二处，
  `common.bind_root_plan` 的 `risk_budget` 回退值就取自它

两处都与拍板的 `ε_prop = 0.20` 冲突。

## 四、✅ 尸体依赖已清除（2026-09-04 抽 common，09-07 完成封存）

原先有 4 处「实验早已结题，但活代码还从它身上借一个符号」，导致这些文件删不掉。
现已把被借的符号集中到 `common.py`（**逐字节搬运**，`inspect.getsource` 逐一比对通过）：

| 原文件 | 被借走的符号 | 现状 |
|---|---|---|
| `run_eval.py` | `DEFAULT_DATA` `_token_delta` `_token_snap` | ✅ 已从活代码移除；S0 快照保留 |
| `p1_policy_certification.py` | `EpisodeResult` `PolicyCandidate` `EpisodeSplit` `stable_episode_split` `clopper_pearson_upper` | ✅ 已从活代码移除；S0 快照保留 |
| `judge_routing_sweep.py` | `judge_j1`（连带 `_PREDECL_RE` `_STOP` `_content_words`） | ✅ 已从活代码移除；S0 快照保留 |
| `run_chronological_p1p2.py` | `bind_root_plan` `wipe_account`（连带 `sha256_text`） | ✅ 已从活代码移除；S0 快照保留（09-07 拆测试后） |

### 混合测试文件已拆（2026-09-07）

`run_chronological_p1p2.py` 之前封不掉，是因为 `tests/test_memebench_planned_propagation.py`
既测活的 `planned_propagation`，又有两个测试借该 runner 的脚手架。已按「剪走」处理：

| 测试 | 实际测什么 | 处置 |
|---|---|---|
| `test_g0_g3_smoke_records_and_both_contracts` | 死 runner 的 `build_case_record` / `json_safe` 记录形状 | 剪走 |
| `test_runner_requires_explicit_models` | 死 runner 自己的 `build_parser` 拒跑缺失模型 | 剪走 |

选「剪走」而非「换脚手架」，因为两者夹带的活断言都已被别处覆盖（拆前逐条核实）：

- `execute_frontier` —— 原文件另有 4 处覆盖
- `point` / `cp-upper` 双 contract —— `test_cp_upper_plan_is_not_point_only`
- 五个 registered build plans —— `test_memebench_chronological_policy.py:31`
- `build_parser` 拒跑缺失模型 —— `test_memebench_chronological_runner.py` 的
  `test_e2e_requires_judge_model` 覆盖同一个 parser，且更完整

剪出的两个测试随历史复现代码保存在 S0 快照分支的
`chronological_p1_certification/tests/test_memebench_chronological_runner_scaffold.py`。
原文件剩 17 测，已零引用死 runner；拆分后两边合计 19 测全过。

### 刻意没做合并

`common.clopper_pearson_upper` 与 `contexthub.planning.statistics.clopper_pearson_upper`
在 60 组随机输入上数值完全一致，但源码不同：生产版优先用 SciPy，benchmark 版刻意不依赖 SciPy。
合并会改变 benchmark run 走哪条代码路径，故两份都保留。

`common.py` 里 `EPSILON_PROP` 用**函数内延迟导入**（`_epsilon_prop()`）：
common 向 `chronological_policy` 提供 dataclass，模块级导入会成环。
用 import 而非复制，保证该常量仍只有一处定义。

## 五、数据文件（不是脚本，别跟着归档）

| 文件 | 谁在用 |
|---|---|
| `abs_excluded_episodes.json` | `run_full100_v3_p2`（11 个排除逐条带理由与证据） |
| `full100_v2_price_table.json` | `run_abs_official_rejudge`（价目表） |
| `runs/` | 全部产物（**已 gitignore**） |
| `adjudications/` | 裁决产物 |
| `RESULTS.md` | 结果记录 |

## 六、✅ 两个孤儿已安置（2026-09-07）

| 文件 | 去处 | 理由 |
|---|---|---|
| `metrics.py` | S0 快照 `shared/` | 两个原用户 `run_eval`（chronological 家族）和 `recompute_cost`（p1_build_sweeps 家族）**分属不同家族**，放进任一家族都会让另一家族的还原不完整，故在快照中使用共享目录 |
| `chronological_ingest.py` | S0 快照 `chronological_p1_certification/` | 唯一活引用者是 `run_chronological_p1p2`，两者同批移出活代码 |

`metrics.py` 是叶子模块（不 import 任何 memebench 模块），所以共享目录不会引入新的跨家族依赖。

## 七、已知的文档不一致

`WORKING-NOTES-p2-semantic-recompute.md` 第 890 行写
「`cluster_inference.py`（bootstrap 机器，第 1 步复用）」——
**实际不成立**：`calibration_cluster_bootstrap.py` 并不 import 它，
而是 import `propagation_planner_eval` + `contexthub.planning.statistics.clopper_pearson_upper`。
`cluster_inference.py` 是 8/19 那次 2×2 聚类推断的独立脚本，已移出活代码并保存在
S0 快照的 `stats_synthetic/` 中。改它不影响 δ。
