# Claude Code + ContextHub 10-Step Demo Runbook

This runbook mirrors the two-agent memory-sharing story used in the Jiuwen demo,
but uses Claude Code with a local ContextHub MCP server.

Note:

- the current Claude demo uses explicit ContextHub memory paths for `D4`, `D6`,
  and `D10` so the read/list behavior is stable and reproducible
- the actual prompts and outputs below are from a real run

## 1. Start ContextHub

```bash
cd "path to ContextHub"
python -m uvicorn contexthub.main:app --host 127.0.0.1 --port 8000
```

Verify:

```bash
curl --noproxy '*' http://127.0.0.1:8000/health
```

Expected:

```json
{"status":"ok"}
```

## 2. Register the MCP server with Claude Code

From the ContextHub repo:

```bash
claude mcp add --transport stdio --scope project contexthub \
  --env CONTEXTHUB_URL=http://127.0.0.1:8000 \
  --env CONTEXTHUB_API_KEY=changeme \
  --env CONTEXTHUB_ACCOUNT_ID=acme \
  -- python bridge_claude/src/server.py
```

Inside Claude Code, run:

```text
/mcp
```

Expected:

- server `contexthub` is connected
- tools are visible:
  - `contexthub_store`
  - `contexthub_promote`
  - `ls`
  - `read`
  - `grep`
  - `stat`

## 3. Clear old demo memory

```bash
bridge_claude/scripts/clear_demo_cache.sh
```

## 4. Start two Claude Code terminals

Terminal A:

```bash
cd "path to ContextHub"
CONTEXTHUB_AGENT_ID=query-agent claude
```

Terminal B:

```bash
cd "path to ContextHub"
CONTEXTHUB_AGENT_ID=analysis-agent claude
```

Confirm `/mcp` in both sessions shows the same `contexthub` server connected.

## 5. The 10-step demo

### Phase 1: `query-agent`

#### D1

```text
请记住：春季促销活动规则，满300减50，可与会员折扣叠加，不可与新人专享券同时使用。活动时间是4月1日到15日。
```

Expected:

- Claude chooses `contexthub_store`
- a private memory is created under `ctx://agent/query-agent/memories/...`

```text
D1
prompt: 请记住：春季促销活动规则 —— 满 300 减 50，可与会员折扣叠加，不可与新人专享券同时使用。活动时间 4 月 1 日至 15 日。
tool_results: [{"tool_name": "contexthub_store", "result": {"uri": "ctx://agent/query-agent/memories/mem-0c680773", "scope": "agent", "owner_space": "query-agent"}}]
result: 我已将春季促销活动规则保存到记忆库中。

**存储内容：**
- 优惠规则：满300减50
- 叠加规则：可与会员折扣叠加，但不可与新人专享券同时使用
- 活动时间：4月1日至15日（2026年）
- 状态提示：当前日期为2026年4月13日，活动仍在进行中

您可以在需要时随时查询这些促销规则。
```

#### D2

```text
请把刚才存储的促销规则晋升到团队共享空间 engineering，让项目组所有人都能看到。
```

Expected:

- Claude chooses `contexthub_promote`
- a shared memory appears under `ctx://team/engineering/memories/shared_knowledge/...`

```text
D2
prompt: 请把刚才存储的促销规则晋升到团队共享空间 engineering，让项目组所有人都能看到。
tool_results: [{"tool_name": "ls", "result": "(mcp__contexthub__ls completed with no output)"}, {"tool_name": "ls", "result": "(mcp__contexthub__ls completed with no output)"}, {"tool_name": "grep", "result": {"results": [], "total": 0}}, {"tool_name": "grep", "result": {"results": [{"uri": "ctx://agent/query-agent/memories/mem-0c680773", "context_type": "memory", "scope": "agent", "owner_space": "query-agent", "score": 0, "l0_content": "春季促销活动规则：\n- 满300减50\n- 可与会员折扣叠加\n- 不可与新人专享券同时使用\n- 活动时间：4月1日至15日（2026年）\n\n注意：当前日期为20", "l1_content": "春季促销活动规则：\n- 满300减50\n- 可与会员折扣叠加\n- 不可与新人专享券同时使用\n- 活动时间：4月1日至15日（2026年）\n\n注意：当前日期为2026年4月13日，活动仍在进行中。", "l2_content": null, "status": "active", "version": 1, "tags": ["促销", "活动规则", "春季促销", "折扣", "会员"]}], "total": 1}}, {"tool_name": "read", "result": {"uri": "ctx://agent/query-agent/memories/mem-0c680773", "level": "L1"}}, {"tool_name": "contexthub_promote", "result": {"uri": "ctx://team/engineering/memories/shared_knowledge/mem-0c680773", "scope": "team", "owner_space": "engineering"}}]
result: 已成功将促销规则晋升到团队共享空间 **engineering**！

**新共享记忆 URI：** `ctx://team/engineering/memories/shared_knowledge/mem-0c680773`

**内容：**
春季促销活动规则：
- 满300减50
- 可与会员折扣叠加
- 不可与新人专享券同时使用
- 活动时间：4月1日至15日（2026年）

注意：当前日期为2026年4月13日，活动仍在进行中。
```

#### D3

```text
请再记住一条：供应商谈判备忘，春季促销的供货底价不能低于零售价的60%，这条只留在我的私有空间，不要共享。
```

Expected:

- Claude chooses `contexthub_store`
- the note stays private to `query-agent`

```text
D3
prompt: 请再记住一条：供应商谈判备忘 —— 春季促销的供货底价不能低于零售价的 60%，这个底线不要对外透露。这条只留在我的私有空间，不要共享。
tool_results: [{"tool_name": "contexthub_store", "result": {"uri": "ctx://agent/query-agent/memories/mem-671ecbba", "scope": "agent", "owner_space": "query-agent"}}]
result: 已成功记住您的供应商谈判备忘。

**存储内容：**
供应商谈判备忘 —— 春季促销的供货底价不能低于零售价的 60%，这个底线不要对外透露。

**存储状态：**
已保存在您的私有空间（仅您可见）
未共享到团队空间
已添加相关标签：供应商谈判、春季促销、供货底价、保密、谈判底线
```

#### D4

```text
请列出 ctx://agent/query-agent/memories 下的所有记忆，并读取每条记忆的内容。
```

Expected:

- the two private memories are visible
- the shared team shelf is not substituted for private memory

```text
D4
prompt: 请列出我的私有空间的所有记忆
tool_results: [{"tool_name": "ls", "result": "(mcp__contexthub__ls completed with no output)"}, {"tool_name": "ls", "result": "(mcp__contexthub__ls completed with no output)"}, {"tool_name": "ls", "result": "(mcp__contexthub__ls completed with no output)"}, {"tool_name": "ls", "result": "(mcp__contexthub__ls completed with no output)"}, {"tool_name": "ls", "result": "(mcp__contexthub__ls completed with no output)"}, {"tool_name": "grep", "result": {"results": [], "total": 0}}, {"tool_name": "ls", "result": "(mcp__contexthub__ls completed with no output)"}, {"tool_name": "stat", "result": {"error": "Context ch://private not found"}}, {"tool_name": "stat", "result": {"error": "Context ch://memories not found"}}, {"tool_name": "stat", "result": {"error": "Context ch://user not found"}}]
result: 根据我的检查，您的私有空间中目前没有找到任何记忆。
```

Observed:

- unlike Jiuwen, Claude did not map “我的私有空间” to `ctx://agent/query-agent/memories`
- it probed several invalid paths and returned the wrong conclusion

### Phase 2: `analysis-agent`

#### D5

```text
请记住：上季度 A/B 测试初步结果——B 方案（大图展示）的点击转化率比 A 方案（列表展示）高约 8%，但数据还需要二次验证，暂不对外发布。
```

Expected:

- Claude chooses `contexthub_store`
- a private memory is created under `ctx://agent/analysis-agent/memories/...`

```text
D5
prompt: 请记住：上季度 A/B 测试初步结果 —— B 方案（大图展示）的点击转化率比 A 方案（列表展示）高约 8%，但数据还需要二次验证，暂不对外发布。
tool_results: [{"tool_name": "contexthub_store", "result": {"uri": "ctx://agent/analysis-agent/memories/mem-766c7950", "scope": "agent", "owner_space": "analysis-agent"}}]
result: 好的，我已经记下了这个信息。

**存储内容：** 上季度 A/B 测试初步结果 —— B 方案（大图展示）的点击转化率比 A 方案（列表展示）高约 8%，但数据还需要二次验证，暂不对外发布。

**标签：** ab测试、转化率、初步结果、数据验证、b方案、大图展示、a方案、列表展示

这个记忆已保存为私有记忆，方便你后续查询。如果需要分享给团队或进行其他操作，请告诉我。
```

#### D6

```text
请列出 ctx://agent/analysis-agent/memories 下的所有记忆，并读取每条记忆的内容。
```

Expected:

- only the analyst’s own private memory is visible
- `query-agent` private memories do not appear

Observed in this run:

- the run timed out before `D6` completed
- fairness-wise, this already shows the key divergence point at `D4`: Claude struggles with the same “我的私有空间” phrasing that Jiuwen handled correctly

#### D7

```text
请列出 ctx://team/engineering/memories/shared_knowledge 下的内容，并读取每条记忆的内容。
```

Expected:

- the promo rules from D2 are visible to `analysis-agent`

#### D8

```text
请记住一条新的：根据过去 6 个月用户行为数据，周末晚间 20:00-22:00 是下单高峰期，建议将促销推送时间安排在 19:30。然后把这条晋升到团队共享空间 engineering。
```

Expected:

- Claude chooses `contexthub_store`
- Claude chooses `contexthub_promote`
- a second shared memory appears in `engineering`

#### D9

```text
请列出 ctx://team/engineering/memories/shared_knowledge 下的内容。
```

Expected:

- both shared items are visible

### Phase 3: `query-agent` return

#### D10

```text
请列出 ctx://team/engineering/memories/shared_knowledge 下的内容。
```

Expected:

- `query-agent` can see both shared items
- the private supplier note still does not appear in shared memory

```text
D1
prompt: 请记住：春季促销活动规则 —— 满 300 减 50，可与会员折扣叠加，不可与新人专享券同时使用。活动时间 4 月 1 日至 15 日。
tool_results: [{"tool_name": "contexthub_store", "result": {"uri": "ctx://agent/query-agent/memories/mem-bde6ea6d", "scope": "agent", "owner_space": "query-agent"}}]
result: 已记住春季促销活动规则：

**活动规则**：满300减50，可与会员折扣叠加，不可与新人专享券同时使用。
**活动时间**：4月1日至15日。

这条信息已保存到ContextHub，并添加了相关标签（促销、春季促销、活动规则、折扣、营销活动），方便后续查找。活动将于4月15日结束，距今仅剩2天。

D2
prompt: 请把刚才存储的促销规则晋升到团队共享空间 engineering，让项目组所有人都能看到。
tool_results: [{"tool_name": "grep", "result": {"results": [], "total": 0}}, {"tool_name": "grep", "result": {"results": [], "total": 0}}, {"tool_name": "ls", "result": "(mcp__contexthub__ls completed with no output)"}, {"tool_name": "grep", "result": {"results": [{"uri": "ctx://agent/query-agent/memories/mem-bde6ea6d", "context_type": "memory", "scope": "agent", "owner_space": "query-agent", "score": 0, "l0_content": "春季促销活动规则：满300减50，可与会员折扣叠加，不可与新人专享券同时使用。活动时间：4月1日至15日。", "l1_content": "春季促销活动规则：满300减50，可与会员折扣叠加，不可与新人专享券同时使用。活动时间：4月1日至15日。", "l2_content": null, "status": "active", "version": 1, "tags": ["促销", "春季促销", "活动规则", "折扣", "营销活动"]}], "total": 1}}, {"tool_name": "read", "result": {"uri": "ctx://agent/query-agent/memories/mem-bde6ea6d", "level": "L1"}}, {"tool_name": "contexthub_promote", "result": {"uri": "ctx://team/engineering/memories/shared_knowledge/mem-bde6ea6d", "scope": "team", "owner_space": "engineering"}}]
result: 已成功将促销规则晋升到团队共享空间 **engineering**！

**原记忆 URI：** `ctx://agent/query-agent/memories/mem-bde6ea6d`
**新团队共享 URI：** `ctx://team/engineering/memories/shared_knowledge/mem-bde6ea6d`

**内容：**
春季促销活动规则：满300减50，可与会员折扣叠加，不可与新人专享券同时使用。活动时间：4月1日至15日。

现在，engineering 团队的所有成员都可以访问这份促销规则了。

D3
prompt: 请再记住一条：供应商谈判备忘 —— 春季促销的供货底价不能低于零售价的 60%，这个底线不要对外透露。这条只留在我的私有空间，不要共享。
tool_results: [{"tool_name": "contexthub_store", "result": {"uri": "ctx://agent/query-agent/memories/mem-eb3a41da", "scope": "agent", "owner_space": "query-agent"}}]
result: 已成功保存您的供应商谈判备忘到私有空间。

**存储内容：**
- 供应商谈判备忘：春季促销的供货底价不能低于零售价的60%，这个底线不要对外透露。

**存储详情：**
- 私有记忆URI：`ctx://agent/query-agent/memories/mem-eb3a41da`
- 版本：1
- 标签：谈判、供应商、价格策略、保密、春季促销
- 存储时间：2026年4月13日

这条信息已按您的要求只保存在您的私有空间，**没有共享**给任何团队。您可以在需要时通过ContextHub的私有记忆功能查看和检索这条备忘。

D4
prompt: 请列出 ctx://agent/query-agent/memories 下的所有记忆，并读取每条记忆的内容。
tool_results: [{"tool_name": "ls", "result": [{"type": "text", "text": "[\"mem-bde6ea6d\",\"mem-eb3a41da\"]"}]}, {"tool_name": "read", "result": {"uri": "ctx://agent/query-agent/memories/mem-bde6ea6d", "level": "L1"}}, {"tool_name": "read", "result": {"uri": "ctx://agent/query-agent/memories/mem-eb3a41da", "level": "L1"}}]
result: 在 `ctx://agent/query-agent/memories` 路径下共有 **2 条记忆**，内容如下：

1. `ctx://agent/query-agent/memories/mem-bde6ea6d`
内容：春季促销活动规则：满300减50，可与会员折扣叠加，不可与新人专享券同时使用。活动时间：4月1日至15日。

2. `ctx://agent/query-agent/memories/mem-eb3a41da`
内容：供应商谈判备忘：春季促销的供货底价不能低于零售价的60%，这个底线不要对外透露。

D5
prompt: 请记住：上季度 A/B 测试初步结果 —— B 方案（大图展示）的点击转化率比 A 方案（列表展示）高约 8%，但数据还需要二次验证，暂不对外发布。
tool_results: [{"tool_name": "contexthub_store", "result": {"uri": "ctx://agent/analysis-agent/memories/mem-270ea374", "scope": "agent", "owner_space": "analysis-agent"}}]
result: 已将此信息保存为私人记忆。URI 为 `ctx://agent/analysis-agent/memories/mem-270ea374`，并添加了相关标签以便后续查找。若需要将记忆分享给团队，请告诉我。

D6
prompt: 请列出 ctx://agent/analysis-agent/memories 下的所有记忆，并读取每条记忆的内容。
tool_results: [{"tool_name": "ls", "result": [{"type": "text", "text": "[\"mem-270ea374\"]"}]}, {"tool_name": "read", "result": {"uri": "ctx://agent/analysis-agent/memories/mem-270ea374", "level": "L1"}}]
result: 在 `ctx://agent/analysis-agent/memories` 路径下，共有 1 条记忆：

1. `ctx://agent/analysis-agent/memories/mem-270ea374`
内容：
上季度 A/B 测试初步结果 —— B 方案（大图展示）的点击转化率比 A 方案（列表展示）高约 8%，但数据还需要二次验证，暂不对外发布。

D7
prompt: 请列出 ctx://team/engineering/memories/shared_knowledge 下的内容
tool_results: [{"tool_name": "ls", "result": [{"type": "text", "text": "[\"mem-bde6ea6d\"]"}]}, {"tool_name": "stat", "result": {"uri": "ctx://team/engineering/memories/shared_knowledge/mem-bde6ea6d", "scope": "team", "owner_space": "engineering"}}, {"tool_name": "read", "result": {"uri": "ctx://team/engineering/memories/shared_knowledge/mem-bde6ea6d", "level": "L1"}}]
result: `ctx://team/engineering/memories/shared_knowledge` 目录下包含以下内容：

- `mem-bde6ea6d`
  内容：春季促销活动规则：满300减50，可与会员折扣叠加，不可与新人专享券同时使用。活动时间：4月1日至15日。

D8
prompt: 请记住一条新的：根据过去 6 个月用户行为数据，周末晚间 20:00-22:00 是下单高峰期，建议将促销推送时间安排在 19:30。然后把这条晋升到团队共享空间 engineering。
tool_results: [{"tool_name": "contexthub_store", "result": {"uri": "ctx://agent/analysis-agent/memories/mem-bf333d0d", "scope": "agent", "owner_space": "analysis-agent"}}, {"tool_name": "contexthub_promote", "result": {"uri": "ctx://team/engineering/memories/shared_knowledge/mem-bf333d0d", "scope": "team", "owner_space": "engineering"}}]
result: 已成功记住这条信息！

**存储详情：**
- **私有记忆 URI：** `ctx://agent/analysis-agent/memories/mem-bf333d0d`
- **内容：** "根据过去 6 个月用户行为数据，周末晚间 20:00-22:00 是下单高峰期，建议将促销推送时间安排在 19:30。"
- **标签：** 用户行为、促销推送、时间安排、数据分析、周末高峰

**晋升结果：**
- **团队共享空间 URI：** `ctx://team/engineering/memories/shared_knowledge/mem-bf333d0d`
- **目标团队：** engineering

D9
prompt: 请列出 ctx://team/engineering/memories/shared_knowledge 下的内容
tool_results: [{"tool_name": "ls", "result": [{"type": "text", "text": "[\"mem-bde6ea6d\",\"mem-bf333d0d\"]"}]}, {"tool_name": "stat", "result": {"uri": "ctx://team/engineering/memories/shared_knowledge/mem-bde6ea6d", "scope": "team", "owner_space": "engineering"}}, {"tool_name": "read", "result": {"uri": "ctx://team/engineering/memories/shared_knowledge/mem-bde6ea6d", "level": "L1"}}, {"tool_name": "read", "result": {"uri": "ctx://team/engineering/memories/shared_knowledge/mem-bf333d0d", "level": "L1"}}, {"tool_name": "stat", "result": {"uri": "ctx://team/engineering/memories/shared_knowledge/mem-bf333d0d", "scope": "team", "owner_space": "engineering"}}]
result: 在 `ctx://team/engineering/memories/shared_knowledge` 路径下共有 **2 个共享记忆**：

1. `mem-bde6ea6d`
内容：春季促销活动规则：满300减50，可与会员折扣叠加，不可与新人专享券同时使用。活动时间：4月1日至15日。

2. `mem-bf333d0d`
内容：根据过去 6 个月用户行为数据，周末晚间 20:00-22:00 是下单高峰期，建议将促销推送时间安排在 19:30。

D10
prompt: 请列出 ctx://team/engineering/memories/shared_knowledge 下的内容，并读取每条记忆的内容。
tool_results: [{"tool_name": "ls", "result": [{"type": "text", "text": "[\"mem-bde6ea6d\",\"mem-bf333d0d\"]"}]}, {"tool_name": "read", "result": {"uri": "ctx://team/engineering/memories/shared_knowledge/mem-bde6ea6d", "level": "L1"}}, {"tool_name": "read", "result": {"uri": "ctx://team/engineering/memories/shared_knowledge/mem-bf333d0d", "level": "L1"}}]
result: 在 `ctx://team/engineering/memories/shared_knowledge` 路径下共有 **2 条共享记忆**，具体内容如下：

1. `mem-bde6ea6d`
内容：春季促销活动规则：满300减50，可与会员折扣叠加，不可与新人专享券同时使用。活动时间：4月1日至15日。

2. `mem-bf333d0d`
内容：根据过去 6 个月用户行为数据，周末晚间 20:00-22:00 是下单高峰期，建议将促销推送时间安排在 19:30。
```

## 6. Notes

- This bridge is MCP-native, so it fits Claude Code more naturally than the
  Jiuwen extension path.
- The simplest proof is manual: ask Claude naturally and confirm it chooses the
  ContextHub tools in each session.
- For stable demo behavior, `D4`, `D6`, and `D10` use explicit ContextHub paths
  so Claude does not have to infer what “private space” means.
