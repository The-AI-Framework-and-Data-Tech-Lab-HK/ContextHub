# EntCollabBench 调研发现

## 来源与本地验证状态

- 源码：<https://github.com/yutao1024/EntCollabBench>
- 数据集：<https://huggingface.co/datasets/Kirito-Lab/EntCollabBench>
- 调研 / 执行 clone：`/Users/sherrylin/Documents/PythonProjects/public/EntCollabBench`
- 源码 commit：`9d085fcb86adaf20254c09e2ca35123e535a9643`
- 数据文件已从 Hugging Face 下载到外部 clone 的 `scripts/dataset/`：
  - `mcp_tasks_160.json`
  - `mcp_multi_tasks_40.json`
  - `approval_tasks_80.json`
  - `approval_multi_task_20.json`

本机未跑通完整 sample。原因不是代码结构不可达，而是运行前置缺失：

- 本机没有 `docker` 命令，无法启动 `Arena/docker-compose-mcp.yml` 的 8 个 MCP 服务和 `agent/docker-compose.yml` 的 11 个 agent 服务。
- 系统 `python3` 是 3.9.6，README 要 Python 3.11；Anaconda `python` 是 3.13.9。外部 clone 中创建 venv 并安装 `requirements.txt` 后，`scripts/benchmark.py --help` 可运行。
- 最小 probe 命令在发起任务前 fail-fast：缺 `JUDGE_OPENAI_API_KEY`/`JUDGE_OPENAI_BASE_URL` 或 fallback 的 `OPENAI_*`，以及 `JUDGE_MODELS`。

已执行的关键命令：

```bash
git clone https://github.com/yutao1024/EntCollabBench.git /Users/sherrylin/Documents/PythonProjects/public/EntCollabBench
cd /Users/sherrylin/Documents/PythonProjects/public/EntCollabBench
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/benchmark.py --help
.venv/bin/python scripts/benchmark.py \
  --tasks-spec-file scripts/dataset/mcp_tasks_160.json \
  --sample-k 1 --sample-seed 1 \
  --skip-seed --skip-cleanup --no-state-diff \
  --batch-concurrency 1 --task-timeout-seconds 3 \
  --agent-url-map-file config/agent_url_map.json \
  --bench-result-jsonl scripts/result/task7_probe_result.jsonl \
  --trajectory-run-jsonl scripts/result/task7_probe_traj.jsonl \
  --trajectory-full-mode --continue-on-error
```

最后一条命令返回：`Missing required judge env: API key, base URL, JUDGE_MODELS.`

## 关键问题回答

### Agent 循环形态

EntCollabBench 的 agent runtime 在 `agent.py`。每个角色是一个独立 HTTP 服务，入口是 `POST /v1/agent/tasks`，请求/响应契约在 `api_schema.py`。`AgentRuntime._invoke_langchain()` 使用 LangChain `create_agent(...).stream(..., stream_mode="updates")` 或 fallback `invoke()` 执行。

跨 agent 委派是结构化 HTTP tool，而不是纯自然语言旁路。`AgentRuntime._create_http_delegation_tool()` 为每个 peer 生成 `ask_{target_agent_name}_by_http` 工具，发送 payload：

- `request_id`
- `source_agent`
- `target_agent`
- `task`
- `recursion.depth/max_depth/trace`
- `metadata.session_id`

可挂钩点：

- `delegate_start`/`delegate_done`/`delegate_error` 事件：ContextHub `Boundary.HANDOFF`
- `tool_call`/`tool_result` 事件：ContextHub `Boundary.TOOL_CALL`
- assistant 最终文本和 `build_success_response()`：ContextHub `Boundary.CLOSURE`

### Role / permission 定义

11 个角色定义在 `config/agent_specs.json`，角色 prompt 中声明身份、专属服务和输出格式。角色到工具权限由 `tool/toolsets.py` 固化：

- IT：`it_service_desk_l1`、`it_change_engineer`，可用 common MCP + `itsm`
- Human Resources：`hr_service_specialist`，可用 common MCP + `hr`
- Customer Service：`customer_support_specialist`，可用 common MCP + `csm`
- Shared Services：`knowledge_base_specialist` 可用 common MCP + ITSM/HR/CSM knowledge-only；`collaboration_ops_specialist` 仅 common MCP
- Engineering：`developer_engineer`、`qa_test_engineer`，可用 common MCP + `gitea`
- Approval Center：`finance_approval_specialist`、`legal_approval_specialist`、`procurement_approval_specialist`，只用 workspace file read/list

服务端身份 token 在 `config/mcp_auth_by_agent.json`，由 `tool/tool_executor.py` 的 `SERVER_REQUIRED_AUTH_HEADER` 转成每个 MCP 服务需要的 header。也就是说权限有两层：agent 可见工具集合 + MCP 服务端 token/header。

### Tool schema

EntCollabBench 不在仓内静态 vendoring 每个 MCP tool schema。schema 来自运行中的 MCP 服务：

- `tool/mcp_bridge.py` 通过 MCP `tools/list` 获取工具列表。
- `MCPBridge.get_tool_schema(server, tool_name)` 从 `inputSchema` 返回 `tool_name/title/description/inputSchema`。
- agent 可见工具包装在 `tool/langchain_tools.py` 中：`mcp_{server}_list_tools`、`mcp_{server}_get_tool_schema`、`mcp_{server}_call_tool`。

Task 8 world_loader 应优先在服务启动后调用 `get_tool_schema` 把 `inputSchema` 写入 ContextHub；没有服务时只能用 dataset `ground_truth[].arguments` 推导临时字段集合，不应冒充完整 schema。

### Policy / approval

approval 文档在 `local_data/{finance,legal,procurement}_approval_specialist/`：

- `rulebook.md`
- `policy_docs/`
- `submission/T-xxxx/` 或 `submission/MT-xxxx/`

任务 ground truth 在 `approval_tasks_80.json` 和 `approval_multi_task_20.json` 的 `ground_truth_approval_results`。每个审批角色的 oracle 包含：

- `decision`
- `rationale`
- `rule_citations`
- 可选 required docs/preapproval 语义，通常编码在 rationale 中

`scripts/judge/judge.py` 对 approval 的检查仍调用 judge LLM 比较 terminal decision 与 ground truth，不是本地纯 deterministic 函数。论文写了 deterministic policy adjudication，但当前开源代码暴露给 benchmark runner 的主要接口是 ground truth + judge 逻辑。

### 业务对象与状态

Workflow 任务操作 8 个企业系统：`calendar`、`csm`、`drive`、`email`、`gitea`、`hr`、`itsm`、`teams`。seed SQL 位于 `Arena/seed/*/dbs/*.sql`，`scripts/benchmark.py` 通过 `mcp.{server}.seed_database` 为每个 task 创建独立 DB。

状态查询接口：

- `tool/mcp_bridge.py::MCPBridge.export_state(server, database_id, tables, where, limit)`
- 底层优先请求 MCP 服务 `/api/export-state`，fallback 到 `/api/database-state`
- 清理使用 `/api/delete-database`

`object_exists` 可由 Task 8 基于 `export_state` 的 table/row 查询实现。对象 URI 建议由 `object_uri("<domain>/<id>")` 生成，例如 `ctx://entcollab/object/hr_case/57` 或 `ctx://entcollab/object/incident/INC000...`。具体 table/主键映射应在 Task 8 连接 MCP 服务后按 `state_export` 或 seed schema 收敛。

### Grader / ground truth

Workflow ground truth 在 dataset 的每个 subtask：

- `ground_truth[].mcp_server_name`
- `ground_truth[].tool_name`
- `ground_truth[].agent`
- `ground_truth[].arguments`

`scripts/benchmark.py` 收集：

- 每个 subtask 的 HTTP request/response
- 所有 agent 的 `/v1/agent/sessions/trace`
- 可选 initial/final state snapshot 与 `canonical_diff`

`scripts/judge/judge.py::evaluate_benchmark()` 读取 ground truth、trace events 和 canonical diff 生成 per-agent/subtask/task pass。它需要 `JUDGE_MODELS` 和 OpenAI-compatible judge endpoint。Task 9 若要拿“该步是否真的违规”的 oracle，应从 dataset ground truth 的 expected tool/agent/arguments 与实际 trace 的 `tool_call`/`delegate_start` 对齐；不要把 judge LLM 的 pass/fail 当成细粒度 policy violation oracle。

### 执行入口

推荐入口是直接 Python/CLI 集成，不是 HTTP sidecar：

```bash
python scripts/benchmark.py \
  --tasks-spec-file scripts/dataset/mcp_tasks_160.json \
  --trajectory-full-mode \
  --batch-concurrency 1 \
  --bench-result-jsonl scripts/result/result.jsonl \
  --trajectory-run-jsonl scripts/result/traj.jsonl \
  --continue-on-error
```

直接 Python 集成更适合 Task 8/9，因为可在以下函数附近插入/包裹 hook：

- `_task_request()`：发送 entry agent task 前，可注入 ContextHub runtime refs/context_versions。
- `_fetch_task_traces_from_all_agents()`：统一收集 trace。
- `_capture_state_snapshot()` / `_compute_canonical_diff()`：绑定 state mutation evidence。
- `AgentRuntime._append_session_event()`：最细粒度的 HANDOFF/TOOL_CALL/CLOSURE 事件出口。

### 模型配置

agent 模型通过环境变量：

- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`
- `AGENT_LLM_MODEL`
- `AGENT_SUMMARY_MODEL`

judge 模型通过：

- `JUDGE_OPENAI_API_KEY` 或 `OPENAI_API_KEY`
- `JUDGE_OPENAI_BASE_URL` 或 `OPENAI_BASE_URL`
- `JUDGE_MODELS`（comma-separated，1–3 个；repo 未硬编码型号，README 仅占位 `<model name>`）

Task 9 的 weak/strong 模型轴应设置 `AGENT_LLM_MODEL`；judge 轴应固定，避免把被测模型变化混入评分器变化。

**论文 judge 型号（已从论文正文 §4.3 "Judgment consistency" 确认）**：三模型多数投票 **Gemini-3.1-Pro + GPT-5.4 + Claude-Sonnet-4.6**（Appendix F 给出与人工标注一致性）。对外可比口径必须用这 3 个。

**判分纪律（Phase 4 冻结，详见 task-prompt/phase 4/task9 §4.2.1 与 plan/19 §5.7）**：
- 两层判分：**层 A 任务成功率**用 repo 原生 `judge.py`（纯 LLM-as-a-judge，见下"Grader / ground truth"）；**层 B 违规 oracle** 用 dataset `ground_truth[]` + DB diff 确定性对齐，不调 judge LLM。
- 两阶段省钱：dev 用单模型 `gpt-5.4`（论文 3 之一，**禁用 `gpt-4o`**），定稿用 3 模型对已存 trajectory re-judge（`benchmark.py` 自带 re-judge，只重跑 judge 不重跑 agent）。dev 单判数字标 provisional，不可对外、不可与 3 模型数字混表。
- **过去 `gpt-4o` 单判的 S0 结果（`/Users/sherrylin/Documents/PythonProjects/ContextHub/integrations/entcollabbench/runs`、`/Users/sherrylin/Documents/PythonProjects/public/EntCollabBench/scripts/result/contexthub_*`）不可作论文可比基线**——要保留须用上述 judge 配置重判。
- judge 不进 weak/strong 轴。3 个型号在所用 provider（如 yunwu.ai）的确切调用 slug 定稿前须先验证可调。

## 代表性 trace 标注

完整 runtime trace 因 Docker/judge/model 环境未跑通，未能采集。基于下载的数据集第一条 `mcp_tasks_160.json` 的 ground truth，可预期 runtime trace 对齐如下：

| EntCollabBench 事件 | 示例 | ContextHub Boundary |
|---|---|---|
| delegation tool call | `ask_it_service_desk_l1_by_http` from `hr_service_specialist` | `HANDOFF` |
| service tool call | `hr.update_hr_case` by `hr_service_specialist` | `TOOL_CALL` / state-changing tool also maps to `STATE_MUTATION` evidence |
| downstream delegation | `ask_collaboration_ops_specialist_by_http` from `it_service_desk_l1` | `HANDOFF` |
| closure output | agent final `DONE:..., UNDONE:..., ERROR:...` or approval `DECISION:...` | `CLOSURE` |

## Mapping 表

| EntCollabBench 概念 | ContextHub URI / 字段 |
|---|---|
| role | `ctx://entcollab/role/{role}` |
| role owner_space | `ROLE_TO_DEPARTMENT[role]`，例如 `developer_engineer -> engineering` |
| MCP server/tool schema | base `ctx://entcollab/tool_schema/{server_or_tool}` |
| runtime schema version | `ctx://entcollab/tool_schema/{name}@vN`，只用于 runtime ref |
| approval rule/policy | base `ctx://entcollab/policy/{policy_id}` |
| runtime policy version | `ctx://entcollab/policy/{policy_id}@vN`，只用于 runtime ref |
| business object | `ctx://entcollab/object/{domain}/{object_id}` |
| dataset ground-truth step | `to_tool_contract_fields()` -> `ToolCallContract` 字段 |

重要约束：world_loader 写 `contexts.uri` 时必须使用 base URI，不写 `@vN`。`@vN` 只出现在 handoff/tool/closure payload 的 `context_versions` 或 declared runtime refs 中。

## 在线接入决策（Phase 4 冻结）

最细粒度 hook 在 `AgentRuntime._append_session_event()`，改外部源码被禁止。但 agent 之间、agent 与工具之间都是 HTTP，因此用**代理形态**在网络层在线拦截，无需改 EntCollabBench 源码。

**形态拍板（2026-06-18，见 `phase4_20260708_next_step.md` §1 决策 6）**：独立进程 HTTP 反向代理（**不**包装 MCP/委派 client——agent 与 MCP 均为 Docker 内独立 HTTP 服务，包装 client 等价改源码/改镜像）；身份用**方案 A（每 agent 一监听口，身份由端口定）**；判定全部委托现有 `runtime_wrapper.py::ContextHubRuntimeWrapper` + `interceptor.py::EnforcementInterceptor.apply()` + `mcp_runtime_adapter.py`，代理只做"网络外壳 + 协议翻译"；**observe→enforce 两段**（observe 永远转发、只记决策，量化 false-block 后再翻 enforce）；**S2 分阶段**（先弱 S2 = 内存 world + NoopStaleness + 仅 decision-log；完整 S2 = DB-backed staleness + 旁路 fail-open audit，留 P0-2b）。

代码件（均已单测）：`online_proxy.py`（tool 代理）、`online_handoff_proxy.py`（handoff 代理）、`online_proxy_base.py`（共享脚手架）、`proxy_config.py`（option A 部署生成器）、`decision_log.py`（observe 读数聚合）、`mcp_runtime_adapter.py::ToolSchemaCache`（live schema + per-server 缓存）。

边界状态：

- **tool：真在线，observe 已在真实 fleet 跑通**（2026-06-20）——MCP 服务前架 ContextHub 代理，工具执行前做 pre-dispatch gate；observe 跑 145/146 得 128 条真实 tool-call 决策（全 forwarded、0 错），live MCP schema 经 `ToolSchemaCache` 真实拉取。enforce 模式已单测、真实 fleet 未验。
- **handoff：真在线，机制已落地 + 对源码校准，待 Docker 现场跑**——在 `ask_{agent}_by_http`（`POST /v1/agent/tasks`）委派链路前架代理拦截；block = 合法 `AgentTaskResponse(status="rejected")`（经 `parse_task_response`，调用方得 `Error:` + 记 `delegate_non_ok`，下游 agent 不启动）。单测全绿，真实 fleet 未跑（tool/handoff 同一套基建）。
- **closure：混合**——在线拦 tool/handoff，closure 走事后 / timeout 边界判定（agent 最终 DONE 文本在其内部产出，难在"宣布前"在线拦）。
- **口径**：只有在线拦 tool+handoff 这部分能 claim 下游 success 改善（H1/H2）；纯 post-run replay（如现有 `s2_diagnostic` / `online_s2_pilot`）只能 claim detection，不能 claim 成功率提升。

> 运行手册（Docker fleet 现状、挂代理两条路、observe 命令骨架、踩过的坑）见 `task-prompt/phase 4/phase4_20260708_next_step.md` §5，不在此重复。

## 风险与待确认

- ~~完整 sample 未跑通，缺 Docker~~ **已部分解除（2026-06-20）**：Docker fleet 已起（11 agent + 7 MCP，**无 gitea**；端口/环境见 `phase4_20260708_next_step.md` §5），observe 已跑通 145/146。仍待：enforce 真实验证、真实 would_block/would_repair 数字（上轮 deepseek-v4-pro 慢、被 180s 超时砍，需调大 timeout 或换快模型）。
- MCP tool schema 依赖运行中的服务，不能只靠仓内静态文件完整恢复（已用 `ToolSchemaCache` 在运行时拉取并缓存；实测 MCP `tools/list` 无需 auth）。
- 论文中的 deterministic policy adjudication 在开源 runner 中没有作为纯本地 API 暴露；当前 runner 仍要求 judge LLM（故层 A 任务成功率必须用其原生 3 模型 judge，见上"模型配置"）。
- `object_exists` 的精确 table/primary-key 映射需要在 MCP 服务启动后，通过 `export_state` 和具体 task `state_export` 再确认（P0-2b 装配完整 S2 时落实）。
- handoff 在线代理拦截**机制已实现 + 对源码校准 + 单测全绿**，待 Docker 现场跑（与 tool 同一套基建）；3 个 judge 型号在 provider 上的确切 slug 需定稿前验证。
- per-agent 身份在线未跑（observe 用了共享映射，`agent_id` 失真）；代理 launcher 需守护（后台回收会静默死）；`online_proxy._apply_patch` 的 patch 基底 bug 翻 enforce 前需修。详见 `phase4_20260708_next_step.md` P0-2c。
