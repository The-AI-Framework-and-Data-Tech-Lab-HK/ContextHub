# JiuwenClaw + ContextHub 10-Step Demo Runbook

This guide shows how to run the sequential Jiuwen demo script:

- `path to ContextHub/bridge_jiuwen/scripts/run_prompt_steps_sequential.sh`

It follows the same 10-step story as `path to ContextHub/docs/mvp verification/mvp-video-plan.md`, but uses JiuwenClaw app/web instead of OpenClaw TUI.

## 1. Host the ContextHub service

### 1.1 Install ContextHub dependencies

All `python` / `pip` commands below assume your active environment already contains JiuwenClaw and is the same environment that provides:

- `jiuwenclaw-app`
- `jiuwenclaw-web`

```bash
cd "path to ContextHub"
pip install -U pip
pip install -e ".[dev]"
pip install greenlet
pip install -e sdk/
```

### 1.2 Start PostgreSQL

```bash
docker compose up -d
```

### 1.3 Initialize the database

```bash
python -m alembic upgrade head
```

### 1.4 Start ContextHub server

```bash
cd "path to ContextHub"
python -m uvicorn contexthub.main:app --host 127.0.0.1 --port 8000
```

### 1.5 Verify health

```bash
curl --noproxy '*' http://127.0.0.1:8000/health
```

Expected:

```json
{"status":"ok"}
```

## 2. Install JiuwenClaw

This repository contains the ContextHub extension and demo scripts. It does not contain the JiuwenClaw package source itself, so install JiuwenClaw into the same Python environment that will run this demo.

### 2.1 Install JiuwenClaw into your active Python environment

If you already have JiuwenClaw installed, skip to step 2.2.

Install it with pip:

```bash
pip install -U pip
pip install jiuwenclaw
```

In the environment used for this demo, the installed package is:

- `jiuwenclaw==0.1.8.post1`

`openjiuwen` is installed automatically as a dependency of `jiuwenclaw`, so you do not need a separate install command unless your environment requires one.

The install should provide these Python entrypoints:

- `jiuwenclaw.app`
- `jiuwenclaw.app_web`

and these shell commands:

- `jiuwenclaw-app`
- `jiuwenclaw-web`

### 2.2 Verify the install

```bash
which jiuwenclaw-app
which jiuwenclaw-web
python -m pip show jiuwenclaw
python -c "import jiuwenclaw; print(jiuwenclaw.__file__)"
```

If these commands fail, your current `python` / `pip` is not pointing at the JiuwenClaw environment yet.

## 3. Change `config.yaml`

Edit `path to ~/.jiuwenclaw/config/config.yaml` so Jiuwen loads the ContextHub extension and uses cloud memory mode.

Minimum required settings:

```yaml
memory:
  mode: cloud

react:
  context_engine_config:
    enabled: true

extensions:
  extension_dirs:
    - path to ContextHub
```

Notes:

- `extension_dirs` must point to the parent directory that contains `path to ContextHub/bridge_jiuwen`, not to the extension folder itself.
- The extension entry point is `path to ContextHub/bridge_jiuwen/extension.py`.
- The runtime tool injection is implemented in `path to ContextHub/bridge_jiuwen/src/bridge.py`.

## 4. Start JiuwenClaw app/web

Restart the user services:

```bash
systemctl --user restart jiuwenclaw-app
systemctl --user restart jiuwenclaw-web
```

Watch logs if needed:

```bash
journalctl --user -u jiuwenclaw-app -f
journalctl --user -u jiuwenclaw-web -f
```

Open the UI:

```text
http://127.0.0.1:19001
```

What you want to see in the app log:

- Jiuwen discovers `bridge_jiuwen`
- `[ContextHubJiuwen] initialized enabled=True`
- extension load count is `1`

## 5. Run the MVP verification script first

Before running the 10-step Jiuwen demo, verify that the underlying ContextHub bridge contract is healthy.

Run:

```bash
python bridge_jiuwen/scripts/run_mvp_verification_curl.py
```

This verifies the MVP runtime path through the Jiuwen bridge:

1. `dispatch(contexthub_store)` creates a private memory and returns a memory URI
2. `dispatch(contexthub_promote)` promotes that memory and returns a team URI
3. `assemble()` returns a non-empty `systemPromptAddition`
4. publish/read flow returns the pinned old version together with an advisory about the newer version

Expected:

- the script exits with code `0`
- Step 1 returns a new private memory record with a URI
- Step 2 returns a promoted `ctx://team/engineering/...` URI
- Step 3 returns a non-empty `systemPromptAddition`
- Step 4 returns the pinned old version together with an advisory
- no connection, permission, or runtime contract errors appear

If this step fails, fix the service/runtime issue first before continuing to the 10-step demo.

## 6. Run the sequential 10-step script

### 6.1 Optional: clear demo cache

```bash
"bridge_jiuwen/scripts/clear_demo_cache.sh"
```

### 6.2 Run the full 10-step sequence

```bash
"bridge_jiuwen/scripts/run_mvp_video.sh"
```

What this script does:

1. clears demo memories
2. switches Jiuwen to `query-agent`
3. runs `D1-D4`
4. switches Jiuwen to `analysis-agent`
5. runs `D5-D9`
6. switches Jiuwen back to `query-agent`
7. runs `D10`

## 7. The 10 steps

This is the same story as `path to ContextHub/docs/mvp verification/mvp-video-plan.md`, adapted to JiuwenClaw.

### Phase 1: `query-agent`

#### D1. Store the promo rules

Prompt:

```text
请记住：春季促销活动规则，满300减50，可与会员折扣叠加，不可与新人专享券同时使用。活动时间是4月1日到15日。
```

Expected:

- Jiuwen calls `contexthub_store`
- a new private URI is created under `ctx://agent/query-agent/memories/...`
```
D1
prompt: 请记住：春季促销活动规则，满300减50，可与会员折扣叠加，不可与新人专享券同时使用。活动时间是4月1日到15日。
tool_results: [{"tool_name": "contexthub_store", "result": "{\"id\": \"958043e7-2bb3-4fb1-9c23-1d4316296f79\", \"uri\": \"ctx://agent/query-agent/memories/mem-323659f8\", \"context_type\"..."}]
result: ✅ 已经记住了！春季促销活动规则已保存到我的记忆里：

**活动规则：**
- 🎯 **满减优惠**：满300减50
- 🔄 **叠加规则**：可与会员折扣叠加使用
- ⚠️ **限制规则**：不可与新人专享券同时使用
- 📅 **活动时间**：4月1日到4月15日
- 🏷️ **活动类型**：春季促销

这个规则看起来挺不错的，满300减50的力度不小，还能和会员折扣叠加，对老用户很友好呢！不过要注意不能和新手券一起用，这个限制挺合理的，避免过度优惠。

需要我帮你计算某个订单是否符合条件，或者有其他关于这个促销的问题吗？😊

```
#### D2. Promote the rules to `engineering`

Prompt:

```text
请把刚才存储的促销规则晋升到团队共享空间 engineering，让项目组所有人都能看到。
```

Expected:

- Jiuwen calls `contexthub_promote`
- a shared URI is created under `ctx://team/engineering/memories/shared_knowledge/...`
```
D2
prompt: 请把刚才存储的促销规则晋升到团队共享空间 engineering，让项目组所有人都能看到。
tool_results: [{"tool_name": "contexthub_promote", "result": "{\"id\": \"be0d5f88-a281-477d-9a47-796b51ec189d\", \"uri\": \"ctx://team/engineering/memories/shared_knowledge/mem-323659f8\"..."}]
result: ✅ 完成！春季促销活动规则已经成功晋升到 engineering 团队的共享空间了。

现在项目组的所有成员都可以通过以下URI访问这个规则：
`ctx://team/engineering/memories/shared_knowledge/mem-323659f8`

这样大家在做相关开发、测试或者客服支持时，都能方便地查阅这个促销规则了。团队协作效率+1！

需要我帮你查看一下团队共享空间里还有哪些其他信息吗？

```

#### D3. Store a sensitive private note

Prompt:

```text
请再记住一条：供应商谈判备忘，春季促销的供货底价不能低于零售价的60%，这条只留在我的私有空间，不要共享。
```

Expected:

- Jiuwen calls `contexthub_store`
- Jiuwen does not promote it
- the note remains private to `query-agent`
```
D3
prompt: 请再记住一条：供应商谈判备忘，春季促销的供货底价不能低于零售价的60%，这条只留在我的私有空间，不要共享。
tool_results: [{"tool_name": "contexthub_store", "result": "{\"id\": \"23f12ac4-0274-41d5-909b-59632c7e5281\", \"uri\": \"ctx://agent/query-agent/memories/mem-441469cd\", \"context_type\"..."}]
result: ✅ 已保存！这条供应商谈判备忘已经安全地存储在你的私有空间了：

**供应商谈判备忘 - 春季促销供货底价：**
- 🔒 **保密信息**：供货底价不能低于零售价的60%
- 🎯 **适用范围**：春季促销活动
- 🛡️ **保密级别**：私有，不共享
- 📝 **备注**：这是供应商谈判的重要底线，需要保密

这条信息标记了"商业机密"标签，只有你能访问。我理解这种谈判底价信息确实需要保密，不能随便共享给团队其他人。

这样安排很合理：促销规则共享给团队，但谈判底价这种敏感信息就留在私有空间。需要我帮你查看或管理其他私有记忆吗？

```

#### D4. List `query-agent` private memory

Prompt:

```text
请列出我的私有空间的所有记忆。
```

Expected:

- exactly 2 relevant memories are shown
- the promo rules from `D1`
- the supplier negotiation note from `D3`
```
D4
prompt: 请列出我的私有空间的所有记忆。
tool_results: [{"tool_name": "ls", "result": {"count": 2, "items": ["mem-323659f8", "mem-441469cd"]}}, {"tool_name": "read", "result": {"uri": "ctx://agent/query-agent/memories/mem-323659f8", "level": "L1", "preview": "春季促销活动规则： 1. 满减优惠：满300减50 2. 叠加规则：可与会员折扣叠加使用 3. 限制规则：不可与新人专享券同时使用 4. 活动时间：4月1日到4月15日 5. 活动类型：春季促销"}}, {"tool_name": "read", "result": {"uri": "ctx://agent/query-agent/memories/mem-441469cd", "level": "L1", "preview": "供应商谈判备忘 - 春季促销供货底价： 1. 供货底价限制：不能低于零售价的60% 2. 适用范围：春季促销活动 3. 保密级别：私有，不共享 4. 备注：这是供应商谈判的重要底线，需要保密"}}]
result: 📋 **你的私有空间记忆列表：**

## 1. **mem-323659f8** - 春季促销活动规则
**内容：**
```
春季促销活动规则：
1. 满减优惠：满300减50
2. 叠加规则：可与会员折扣叠加使用
3. 限制规则：不可与新人专享券同时使用
4. 活动时间：4月1日到4月15日
5. 活动类型：春季促销
```
**状态：** ✅ 已晋升到 engineering 团队共享空间

## 2. **mem-441469cd** - 供应商谈判备忘
**内容：**
```
供应商谈判备忘 - 春季促销供货底价：
1. 供货底价限制：不能低于零售价的60%
2. 适用范围：春季促销活动
3. 保密级别：私有，不共享
4. 备注：这是供应商谈判的重要底线，需要保密
```
**状态：** 🔒 仅私有，未共享

---

**总结：**
- 你目前有 **2条** 私有记忆
- 1条已共享给团队（促销规则）
- 1条保持私有（谈判底价）

这样的安排很清晰呢！公开信息共享，敏感信息保密。需要我对这些记忆进行其他操作吗？比如修改、删除或者搜索相关内容？🔍
```

### Phase 2: `analysis-agent`

#### D5. Store the analyst’s private note

Prompt:

```text
请记住：上季度 A/B 测试初步结果——B 方案（大图展示）的点击转化率比 A 方案（列表展示）高约 8%，但数据还需要二次验证，暂不对外发布。
```

Expected:

- Jiuwen calls `contexthub_store`
- a new private URI is created under `ctx://agent/analysis-agent/memories/...`
```
D5
prompt: 请记住：上季度 A/B 测试初步结果——B 方案（大图展示）的点击转化率比 A 方案（列表展示）高约 8%，但数据还需要二次验证，暂不对外发布。
tool_results: [{"tool_name": "contexthub_store", "result": "{\"id\": \"e1503cc6-e787-4de1-8ae2-79b93557f552\", \"uri\": \"ctx://agent/analysis-agent/memories/mem-e0d655db\", \"context_ty..."}]
result: ✅ 已经记住了！我把上季度A/B测试的初步结果保存下来了：

- **B方案（大图展示）** 点击转化率比 **A方案（列表展示）** 高约 **8%**
- **重要提醒**：数据还需要二次验证，暂不对外发布
- **标签**：A/B测试、转化率、产品优化、数据分析、保密信息

这个信息现在安全地保存在我的记忆里了，需要的时候可以随时问我。我会记得这是初步结果，需要进一步验证才能正式使用。🔒

```

#### D6. Verify private isolation

Prompt:

```text
请列出我的私有空间的所有记忆。
```

Expected:

- only the analyst’s own private memory is visible
- the supplier negotiation note from `query-agent` must not appear
```
D6
prompt: 请列出我的私有空间的所有记忆。
tool_results: [{"tool_name": "ls", "result": {"count": 1, "items": ["mem-e0d655db"]}}, {"tool_name": "read", "result": {"uri": "ctx://agent/analysis-agent/memories/mem-e0d655db", "level": "L1", "preview": "上季度 A/B 测试初步结果： - B 方案（大图展示）的点击转化率比 A 方案（列表展示）高约 8% - 数据还需要二次验证，暂不对外发布 - 测试时间：上季度 - 状态：初步结果，待验证"}}]
result: ## 📋 你的私有空间记忆列表

目前你的私有空间中只有 **1 条记忆**：

### 1. **上季度 A/B 测试初步结果**
- **URI**: `ctx://agent/analysis-agent/memories/mem-e0d655db`
- **内容**：
  - B 方案（大图展示）的点击转化率比 A 方案（列表展示）高约 8%
  - 数据还需要二次验证，暂不对外发布
  - 测试时间：上季度
  - 状态：初步结果，待验证
- **标签**: A/B测试、转化率、产品优化、数据分析、保密信息
- **创建时间**: 2026-04-10 15:04:57

看起来这是你刚刚让我记住的那条重要信息！✨ 如果你有其他需要记住的事情，随时告诉我，我会帮你保存到私有空间里。

```

#### D7. Verify shared visibility

Prompt:

```text
请列出 ctx://team/engineering/memories/shared_knowledge 下的内容。
```

Expected:

- the promo rules promoted in `D2` are visible to `analysis-agent`
```
D7
prompt: 请列出 ctx://team/engineering/memories/shared_knowledge 下的内容。
tool_results: [{"tool_name": "ls", "result": {"count": 1, "items": ["mem-323659f8"]}}, {"tool_name": "read", "result": {"uri": "ctx://team/engineering/memories/shared_knowledge/mem-323659f8", "level": "L1", "preview": "春季促销活动规则： 1. 满减优惠：满300减50 2. 叠加规则：可与会员折扣叠加使用 3. 限制规则：不可与新人专享券同时使用 4. 活动时间：4月1日到4月15日 5. 活动类型：春季促销"}}]
result: ## 📋 工程团队共享知识空间内容

在 `ctx://team/engineering/memories/shared_knowledge` 路径下有 **1 条共享记忆**：

### 1. **春季促销活动规则**
- **URI**: `ctx://team/engineering/memories/shared_knowledge/mem-323659f8`
- **内容**：
  1. **满减优惠**：满300减50
  2. **叠加规则**：可与会员折扣叠加使用
  3. **限制规则**：不可与新人专享券同时使用
  4. **活动时间**：4月1日至4月15日
  5. **活动类型**：春季促销

这是工程团队共享的重要业务规则信息，所有团队成员都可以访问和参考。👥

如果你想把你私有空间中的A/B测试结果也分享给工程团队，我可以帮你晋升到共享空间哦！

```

#### D8. Contribute a second shared insight

Prompt:

```text
请记住一条新的：根据过去 6 个月用户行为数据，周末晚间 20:00-22:00 是下单高峰期，建议将促销推送时间安排在 19:30。然后把这条晋升到团队共享空间 engineering。
```

Expected:

- Jiuwen calls `contexthub_store`
- Jiuwen calls `contexthub_promote`
- a second shared item appears in `engineering`
```
D8
prompt: 请记住一条新的：根据过去 6 个月用户行为数据，周末晚间 20:00-22:00 是下单高峰期，建议将促销推送时间安排在 19:30。然后把这条晋升到团队共享空间 engineering。
tool_results: [{"tool_name": "contexthub_store", "result": "{\"id\": \"5ada9360-b9fb-445e-ab70-2438ab240031\", \"uri\": \"ctx://agent/analysis-agent/memories/mem-df52fd3d\", \"context_ty..."}, {"tool_name": "contexthub_promote", "result": "{\"id\": \"2a1ebb9d-cfb9-4d6d-a074-b855809dddfd\", \"uri\": \"ctx://team/engineering/memories/shared_knowledge/mem-df52fd3d\"..."}]
result: ## ✅ 完成！

我已经完成了两个步骤：

### 1. **记住了新的用户行为数据**
- **内容**：根据过去6个月用户行为数据，周末晚间20:00-22:00是下单高峰期，建议将促销推送时间安排在19:30
- **私有记忆URI**: `ctx://agent/analysis-agent/memories/mem-df52fd3d`
- **标签**: 用户行为、数据分析、推送时间、高峰期、运营优化

### 2. **晋升到工程团队共享空间**
- **共享记忆URI**: `ctx://team/engineering/memories/shared_knowledge/mem-df52fd3d`
- **目标团队**: engineering

现在这条重要的运营洞察已经：
1. ✅ 保存在你的私有空间（方便个人参考）
2. ✅ 共享到工程团队空间（所有团队成员都可以看到）

这个时间建议很有价值！🎯 提前30分钟推送，正好在用户开始活跃前触达，能最大化促销效果。

```

#### D9. Verify both shared contributions exist

Prompt:

```text
请列出 ctx://team/engineering/memories/shared_knowledge 下的内容。
```

Expected:

- promo rules from `D2`
- push-time recommendation from `D8`
```
D9
prompt: 请列出 ctx://team/engineering/memories/shared_knowledge 下的内容。
tool_results: [{"tool_name": "ls", "result": {"count": 2, "items": ["mem-323659f8", "mem-df52fd3d"]}}, {"tool_name": "read", "result": {"uri": "ctx://team/engineering/memories/shared_knowledge/mem-323659f8", "level": "L1", "preview": "春季促销活动规则： 1. 满减优惠：满300减50 2. 叠加规则：可与会员折扣叠加使用 3. 限制规则：不可与新人专享券同时使用 4. 活动时间：4月1日到4月15日 5. 活动类型：春季促销"}}, {"tool_name": "read", "result": {"uri": "ctx://team/engineering/memories/shared_knowledge/mem-df52fd3d", "level": "L1", "preview": "根据过去 6 个月用户行为数据分析： - 周末晚间 20:00-22:00 是下单高峰期 - 建议将促销推送时间安排在 19:30（提前30分钟） - 数据来源：过去6个月用户行为数据 - 应用场景：促销活动推送时间优化"}}]
result: ## 📋 工程团队共享知识空间内容

现在 `ctx://team/engineering/memories/shared_knowledge` 路径下有 **2 条共享记忆**：

### 1. **春季促销活动规则** (mem-323659f8)
- **内容**：
  1. **满减优惠**：满300减50
  2. **叠加规则**：可与会员折扣叠加使用
  3. **限制规则**：不可与新人专享券同时使用
  4. **活动时间**：4月1日至4月15日
  5. **活动类型**：春季促销

### 2. **用户行为数据分析 - 推送时间优化** (mem-df52fd3d) ⭐ **刚刚晋升**
- **内容**：
  - 根据过去6个月用户行为数据分析
  - **周末晚间 20:00-22:00** 是下单高峰期
  - **建议将促销推送时间安排在 19:30**（提前30分钟）
  - 数据来源：过去6个月用户行为数据
  - 应用场景：促销活动推送时间优化

---

🎯 **完美搭配！** 这两条信息正好可以结合使用：
1. **春季促销规则** - 知道活动内容和时间
2. **推送时间建议** - 知道最佳推送时机（19:30）

这样工程团队在实施促销活动时，就能在正确的时间推送正确的优惠内容，最大化转化效果！🚀
```

### Phase 3: `query-agent` return

#### D10. Verify shared changes are visible both ways

Prompt:

```text
请列出 ctx://team/engineering/memories/shared_knowledge 下的内容。
```

Expected:

- `query-agent` can now also see the analyst’s push-time recommendation
- the private supplier negotiation note from `D3` still does not appear in shared memory
```
D10
prompt: 请列出 ctx://team/engineering/memories/shared_knowledge 下的内容。
tool_results: [{"tool_name": "ls", "result": {"count": 2, "items": ["mem-323659f8", "mem-df52fd3d"]}}, {"tool_name": "read", "result": {"uri": "ctx://team/engineering/memories/shared_knowledge/mem-323659f8", "level": "L1", "preview": "春季促销活动规则： 1. 满减优惠：满300减50 2. 叠加规则：可与会员折扣叠加使用 3. 限制规则：不可与新人专享券同时使用 4. 活动时间：4月1日到4月15日 5. 活动类型：春季促销"}}, {"tool_name": "read", "result": {"uri": "ctx://team/engineering/memories/shared_knowledge/mem-df52fd3d", "level": "L1", "preview": "根据过去 6 个月用户行为数据分析： - 周末晚间 20:00-22:00 是下单高峰期 - 建议将促销推送时间安排在 19:30（提前30分钟） - 数据来源：过去6个月用户行为数据 - 应用场景：促销活动推送时间优化"}}]
result: 好的！我已经查看了 `ctx://team/engineering/memories/shared_knowledge` 下的内容。这里有两个共享知识条目：

## 1. 春季促销活动规则 (mem-323659f8)
**内容：**
- 满减优惠：满300减50
- 叠加规则：可与会员折扣叠加使用
- 限制规则：不可与新人专享券同时使用
- 活动时间：4月1日至4月15日
- 活动类型：春季促销

## 2. 用户行为数据分析 (mem-df52fd3d)
**内容：**
根据过去6个月用户行为数据分析：
- 周末晚间 20:00-22:00 是下单高峰期
- 建议将促销推送时间安排在 19:30（提前30分钟）
- 数据来源：过去6个月用户行为数据
- 应用场景：促销活动推送时间优化

这两个都是很有价值的团队共享知识呢！第一个是具体的促销活动规则，第二个是基于数据分析得出的运营建议。这些信息对于工程团队在开发相关功能时应该很有参考价值吧？😊
```



## 8. Troubleshooting

### Jiuwen is still running as `jiuwenclaw`

The sequential runner switches agent identity by setting a user-systemd environment variable and restarting services. The shared shell helper is `path to ContextHub/bridge_jiuwen/scripts/demo_common.sh`.

If needed, switch manually:

```bash
systemctl --user set-environment CONTEXTHUB_AGENT_ID=query-agent
systemctl --user restart jiuwenclaw-app jiuwenclaw-web
```

or:

```bash
systemctl --user set-environment CONTEXTHUB_AGENT_ID=analysis-agent
systemctl --user restart jiuwenclaw-app jiuwenclaw-web
```

### ContextHub is down

Check:

```bash
curl --noproxy '*' http://127.0.0.1:8000/health
```

### Jiuwen web is down

Check:

```bash
curl --noproxy '*' http://127.0.0.1:19001/
```

### You only want one phase

Run the phase script directly:

```bash
python -u "path to ContextHub/bridge_jiuwen/scripts/run_agent_prompt_steps.py" --phase query
python -u "path to ContextHub/bridge_jiuwen/scripts/run_agent_prompt_steps.py" --phase analysis
python -u "path to ContextHub/bridge_jiuwen/scripts/run_agent_prompt_steps.py" --phase query-return
```
