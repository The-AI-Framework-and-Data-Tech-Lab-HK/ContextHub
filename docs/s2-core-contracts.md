# S2 共用核心合同（schema v1）

S2 是来源、产物和执行事实的存储层，不启动模型、不实现传播／核验算法、不更改 context 可用性。无 MEME、gold、题型、评分模块依赖。后续 S3–S8 复用这些核心接口。

## 对象与引用

所有 DTO 使用 Pydantic `extra=forbid`、`frozen=True`；集合使用 tuple。`schema_version=1`，未知版本拒绝。普通构造和存储入口都会校验；不要用绕过校验的 `model_construct`。修改产生新 ID。JSON canonical hash = SHA-256(UTF-8 JSON，sort_keys，紧凑分隔，无 ASCII 转义，无 NaN)；文本 hash = SHA-256(原始 UTF-8)，不正规化、不 trim。

`SealRef(account_id,id,kind,sha256)` 绑定租户、UUID、类型和完整 payload 身份。相同内容重新使用同一 ID 幂等；同一 ID 不同内容报 `artifact_hash_mismatch`，不覆盖。数据库触发器拒绝封存记录 UPDATE/DELETE。显式管理员 TRUNCATE/DDL 破坏不属防护目标。

| 对象 | 关键字段与语义 |
| --- | --- |
| SourceSnapshot | context ID/version、content_level、ingested_at、cutoff_at、content_sha256；正文继续在既有 context_versions；不另建内容库 |
| EvidenceRef | source 封存引用、start/end、excerpt/hash、provenance；区间为 Python Unicode code point 的 0-based `[start,end)`，定位于所选 l0/l1/l2 原文 |
| PropositionArtifact | sources、命题及证据、抽取配置 ID/hash、complete/pending 与失败原因；pending 必须有原因，空成功与失败区分 |
| GraphSnapshot | graph_version、唯一 context/version 节点、upstream→downstream 有向依赖、语义依据、证据、provenance、pending；拒绝重复、悬空、自环和有向环 |
| MaintenanceSnapshot | event、target/version、necessary_upstreams、cause event 集合、graph ref、allowed_sources、cutoff、policy hash |
| EvidenceBundle | maintenance ref、scope、required/read 清单、complete/truncated/issues/cursor、ordinal/page/chunk、ordering/page_size/chunk_size、已知冲突；complete 不允许漏读或截断 |
| Candidate | maintenance/bundle 引用、updated/unchanged、拟议文本、证据、generator call/config、可选 rule version；没有 PASS 或提交权限字段 |
| HardCheckResult | candidate、checker version、逐项 code/verdict/reason；执行检查算法留 S6 |
| VerificationResult | candidate/bundle、独立 verifier call/config、applicability/complete_support/conflicts_resolved 各自 PASS/FAIL/UNKNOWN 与原因 |
| CommitProof | candidate/check/verifier/maintenance、提交版本、已核上游/来源/cause、时间/代码；`authority=audit_only`，仅审计回执，不是 freshness 授权凭证 |
| WorkState | CAS revision、pending/running/updated/unchanged/unresolved、reason/proof；与 ContextStatus/validity_status 分开 |

S2 的 proof 存储只验证记录间的一致性、独立调用 ID、PASS、完整 bundle 和版本关系；**不证明语义正确、不执行提交、不移除失效原因**。生成器使用普通 `seal` 无法保存 proof。`archive_commit_proof` 供后续提交路径在同一 ScopedRepo 事务内保存审计回执；调用它也不会使任何内容 fresh。S2 的 `update_work` 不开放 updated/unchanged：返回 `commit_path_not_implemented`，S6 必须实现真正的 guarded commit 后接入，不能把该审计接口当作提交替代。

## 服务接口与事务

- `ArtifactService.seal(db,obj) -> SealRef`，`get(db,ref) -> Sealed`；校验显式引用、来源、租户和版本。未知、缺失、错误 hash 均报错，不隐式回退最新版本。
- `EvidenceService.snapshot(db, account_id, source, content_level, cutoff_at, snapshot_id?)`；`reference(db, source, start, end, provenance)`；`resolve(db, EvidenceRef) -> str`；`read_source` 可重新核对内容版本。
- `ArtifactService.create_work/get_work/update_work` 只维护工作进度；同 snapshot 唯一 work ID，同 ID 重复 create 返回现状；CAS 失败报 `work_revision_conflict`。终态不得重开，后续事件使用新 snapshot/work ID。
- `archive_commit_proof(db,proof)` 追加审计回执，`get` 用 commit_proof ref 读取。表和接口没有任何更新 contexts 的 SQL。
- 上述使用现有 `PgRepository.session(account_id)`／`ScopedRepo`，随调用者事务提交或回滚。context_versions 没有自身 RLS，因此所有来源读取都 join contexts 并加 account 过滤。
- `ExecutionLedger(repository).append(record)` 每次单独提交，保证业务事务回滚不丢失真实调用成本；`records(account_id,call_id)` 顺序返回全部事件，空 tuple 表示当前租户无记录。账本不复用调用者的长事务连接。

四张新增表：knowledge_artifacts、maintenance_work_items、execution_attempts、context_refresh_proofs。全部 FORCE RLS，显式 USING/WITH CHECK；前三类封存表（除可 CAS 更新的 work 表）有禁止修改触发器。迁移内部链是 001→002→003→004→005→006→007→008→009→010；旧文件名中 002/003/004 不代表实际 006/007/008。010 降级只删除四张 S2 表及其触发函数；不恢复 009 删除的 recompute。

## 逐次调用及重试

```python
client = OpenAIChatClient(api_key, base_url=endpoint, model=explicit_model)
ledger = ExecutionLedger(repository)
call = RecordedCall(identity=call_identity, policy=retry_policy, recorder=ledger,
                    price=optional_price_basis, validate_response=optional_schema_validator)
text = await client.complete(prompt, max_tokens=limit, call=call)
```

新入口要求显式 model、RetryPolicy 和 recorder。RetryPolicy 必填 max_attempts（1–3）、timeout_seconds、backoff_seconds（长度 max_attempts−1）、retry_status_codes 和 retry_transport_errors；没有正式运行默认值。只允许声明 429/5xx 和支持的临时 transport 异常。解析、schema、空内容及 validator 确定失败均不做同级重试；语义 FAIL/UNKNOWN 可由后续调用者处理，不在 transport 层重试。后续上层必须复用同一个逻辑 call ID，不包第二层重试；新级别／动作才分配新 ID。

每次真实发送前写 `started`，随后写 `finished`。唯一键 `(account_id,call_id,attempt_no,phase)`；start 是不可重复的预约，finish 完全相同的重写幂等，不同内容拒绝。按 call 做数据库事务锁和序号检查；不会从全局累计 usage 相减。前次只有 `retry_scheduled` 才允许下一 attempt；三次后不能再排重试。同 call 身份、输入、prompt、配置、策略、价格不允许漂移。并发重入和外围同 call 重试会在网络前拒绝。

AttemptRecord 保存 execution/call/event/work ID、策略/阶段/操作、角色、代码/config、实际请求/prompt/retry-policy hash、requested/actual model、provider request ID、attempt_no、UTC 起止时间、monotonic wall time、错误类/码/retryable/停止原因、每次 usage/价格/费用和可选本地工作量。completion ID 不冒充 request ID。provider 未返回身份时分别保存缺失原因。错误消息、响应正文、鉴权头和密钥不进入账本。

Usage 区分 complete/partial/missing/inconsistent；未知 token 不填 0。价格保存来源/日期/币种/地区/层级/模型/单位/费率和 hash；只有 usage 完整、模型匹配、所需费率都有时，才按 Decimal 复算 exact 金额。其他情况 amount=null、status=unknown 并给原因。S2 支持明确的输入/缓存输入/输出 token 费率；其他计价形态留 unknown，不伪造精确估计。

网络之前账本失败则零请求；完成记录失败则停止后续发送，已持久化 start 保留为未完成记录，不能解释为成功或免费。取消也尽力写 finished/cancelled。进程硬退出或数据库不可用时不能保证完成记录写入，单独的 start 暴露这个缺口；生产级崩溃恢复不属 S2。

不传 `call` 的旧 `complete(prompt,max_tokens)` 保留原响应解析、最多四次尝试及 `last_usage/last_attempts` 行为，供旧调用者兼容。这些旧聚合属性不是新流程的并发账本。替身 transport 通过构造器注入；正式装配不得再给 transport 加隐式重试。

## 错误与后续接入

`ContractError.code` 是程序分类；DTO 形状和跨字段校验通过 Pydantic ValidationError 拒绝（其中包含原合同码）。常用码：

| 类别 | 错误码 |
| --- | --- |
| 租户/来源 | tenant_mismatch、source_version_missing、source_content_missing、source_version_mismatch、source_after_cutoff、source_span_mismatch |
| 封存/引用 | artifact_missing、artifact_hash_mismatch、artifact_identity_mismatch、reference_kind_mismatch、context_version_missing、event_missing、cause_event_missing |
| 清单/关联 | incomplete_manifest、duplicate_manifest_entry、source_outside_scope、maintenance_reference_mismatch、evidence_not_read、bundle_reference_mismatch |
| 工作/证明 | work_missing、work_identity_conflict、work_revision_conflict、generator_cannot_verify、proof_checks_not_passed、proof_version_mismatch、commit_path_not_implemented |
| 逐次执行 | attempt_already_recorded、attempt_sequence_mismatch、call_identity_mismatch、call_closed_or_incomplete、attempt_start_missing、retry_limit_exceeded、recorded_call_model_required |

S3 封存抽取结果；S4 封存带证据的 DAG；S5 决定失效屏障、允许来源及 required 清单，再保存快照/bundle；S6 实现核验算法与提交权限/漂移检查，原子接入证明与工作终态；S7 装配配置/API/SDK；S8 只做实验归属/导出，不重复计费。source ingested_at 当前用既有 context_versions.created_at；若适配器有逻辑摄入顺序，须在 S5 的 allowed_sources/排序清单中显式固定，不拿截止时间替代完整 manifest。

## 验证入口

单元：`CONTEXTHUB_INTEGRATION=0 .venv/bin/python -m pytest -q tests/test_knowledge_{contracts,artifacts,ledger,retry}.py tests/test_chat_client_retry_usage.py`。

数据库：先在独立本地 PostgreSQL（带 pgvector）上跑全链迁移；设置显式 `S2_TEST_DATABASE_URL`，数据库名 `contexthub_s2*`、角色 `s2_*` 且 NOSUPERUSER/NOBYPASSRLS、明确非 5432 端口。仅运行 `CONTEXTHUB_INTEGRATION=1 .venv/bin/python -m pytest -q tests/test_integration_knowledge_storage.py`。fixture 无默认地址、不调用旧 db_pool/clean_db、不 TRUNCATE，每个用例使用独立租户 UUID。初始建库/扩展/迁移可用专用实例管理员，实际合同必须用非超级用户。具体本次建立方式、命令和结果见研究仓 S2-implementation 交接。
