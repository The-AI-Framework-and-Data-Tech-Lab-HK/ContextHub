# Updated-model live validation — 2026-11-03

The explicit Deep Agents adapter passed all ten real tool calls with the user's
updated model configuration. Supplemental checks of model-generated document
trees **did not pass**. Successful document reads used the server's existing
fallback sectioning.

This is a new run; the [earlier report](live-validation-2026-11-03.md) is retained
as historical evidence.

## Configuration and isolation

The run loaded the updated `.env` from the original ContextHub checkout. Both
the agent and the server used `deepseek-v4-pro-0813` through
`https://dashscope-intl.aliyuncs.com/compatible-mode/v1`. The earlier DeepSeek
official-endpoint fallback was not used. Responses identified the updated model,
and no quota or insufficient-balance error occurred.

Embeddings remained `text-embedding-v4`. A direct probe returned 1024 dimensions;
the existing server client pads those vectors to the configured 1536 dimensions.
All 12 contexts from this run's attempts had vectors in PostgreSQL.

The environment used the same Deep Agents 0.7.32, LangChain 1.4.8, and
langchain-core 1.7.4 versions as the earlier run. A new temporary database was
migrated through revision 010. The application role was neither superuser nor
allowed to bypass row-level security. Test agents had their own account and team
memberships. Lifecycle/propagation background workers remained disabled.

## Adapter confirmation

Successful run ID: `8534e5f9afb4`.
Time: 2026-11-03 14:33:14–14:34:52 Hong Kong time (UTC+8), 98.08 seconds including
fixture creation and assertions.

| Workflow or check | Result |
|---|---|
| Search → full source read → adopted feedback with the original retrieval ID | Passed |
| Explicit private memory save → team promotion → another member's exact-content read | Passed |
| Another agent reading private memory | HTTP 403, passed |
| Non-member reading team memory | HTTP 403, passed |
| Same agent ID under a different account | HTTP 404, passed |
| Document list → metadata → section list → original section read, using `astream` | Passed using fallback-generated sections |
| Skill v2 publication, exact v2 content, and preserved v1 content | Passed |
| Invalid ContextHub API key propagated to the real model | HTTP 401, passed |

All ten `contexthub_*` tools ran through the real model graph. The three isolation
checks additionally invoked adapter tools directly and verified their error
status and messages. The final model answers contained generated facts that had
not been included in the questions.

The provider-reported agent usage was 83,384 input tokens and 2,302 output tokens
(85,686 total). This covers the successful agent workflows only, excluding
earlier attempts, server-side document generation, embeddings, and diagnostic
probes. It is not a cost estimate.

### Live-check prompt correction

The first attempt (`3afd1207f9bc`) correctly transported the skill publication,
but the model included the subsequent verification instruction in the stored
skill text. The test had placed that instruction immediately after
`with this exact content:` without a boundary. Its exact-content assertion
correctly failed.

The live-check example now specifies the content as a JSON string and separates
the verification instructions into their own paragraph. The exact-content
assertion was retained, and the full rerun passed. No adapter or server runtime
behavior was changed for this correction. Ruff checks, formatting, and
`git diff --check` passed. The earlier unit/DB regression suites were not rerun
for this example-prompt/documentation-only change.

## Supplemental section-tree checks

ContextHub's section tree is a document directory with titles, parents, summaries,
and source-character ranges. The existing `sections` and `read_section` tools
consume it; constructing that tree happens in the server's ingestion service.

A temporary observer delegated to the real chat client and original tree/fallback
functions, recording their outputs and selected paths. It did not substitute
model responses or alter return values.

- The default server request used `max_tokens=2000`. A recorded HTTP 200 response
  used that completion budget but returned no final text, and the service fell
  back to heading parsing.
- A nested Markdown document retained its intended hierarchy and facts through
  heading parsing, but failed the requirement to use a model-generated tree.
- A Chinese plain-text document fell back to sequential chunks and did not
  expose the expected preparation/recovery sections. This check also failed.
- One diagnostic request on the same model with `enable_thinking=false`
  returned complete JSON. However, its source ranges misplaced the section
  boundary and omitted a required duration sentence. The response passed the
  existing structural validator, so disabling thinking alone is insufficient.
  This diagnostic used the same prompt and output limit, without changing the
  user's saved configuration. The provider documents the
  [thinking-mode switch](https://docs.bailian.console.aliyun.com/en/model-studio/deepseek-api).

The issues and concrete reproduction evidence are recorded separately in
[Reliable model-generated document sections](document-tree-generation-followup.md).
Fix that path before relying on it for document-retrieval benchmarks. This run
does not claim broad document parsing accuracy or validate automatic memory.

## Evidence and cleanup

Original local artifacts are under
`/private/tmp/contexthub-deepagents-recheck-20261103/`:

- `adapter-confirmation/report.json` and the five workflow JSON traces;
- `adapter-run/` for the initial failed exact-content assertion;
- `tree-check/report.json`, its synthetic fixtures, and `tree-traces/`;
- `nonthinking-probe.json` and `nonthinking-span-check.json`;
- `database-verification.json` and `server.log`.

Some initial document calls overlapped; shared mutable chat-client usage counters
were not treated as reliable per-document accounting for those calls.

The temporary service, database, role, and server document-store directory were
removed after inspection. Reports, synthetic fixtures, and traces were retained.
No existing research data or user configuration file was modified.
