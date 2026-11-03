# Deep Agents live validation — 2026-11-03

Result: all ten adapter tools completed real model-driven calls against the
ContextHub HTTP server and PostgreSQL. Five agent workflows and three direct
permission checks passed. This validates integration behavior on synthetic data;
it does not measure retrieval accuracy or automatic memory quality.

## Environment

- Branch: `codex/deep-agents-adapter`, based on upstream commit
  `eaf901c388c57b78a6349644be44105a4f4626ea`.
- Python 3.13; `deepagents==0.7.32`, `langchain==1.4.8`,
  `langchain-core==1.7.4`, `langchain-openai==1.7.3`.
- Agent model: `deepseek-flash` through the existing DeepSeek official endpoint,
  using LangChain's OpenAI-compatible client. Responses reported `deepseek-flash`.
- Server embeddings: DashScope `text-embedding-v4`, stored in 1536-dimensional
  pgvector columns. All nine contexts remaining from the live attempts had
  embeddings; neither the embedding client nor the model was mocked.
- Separate temporary database, migrations 001–010, and a dedicated application
  role with `rolsuper = false` and `rolbypassrls = false`.
- Test account `adapter-live`, agents `writer` and `reader`, writable team
  `adapter-test`, and root-team memberships for document access. Lifecycle and
  propagation background workers were disabled; they were not under test.
- Successful run: `8e04404e0bae`, 11:52:42–11:53:08 Hong Kong time (UTC+8),
  approximately 25.77 seconds including fixture setup and assertions.

## Results

| Check | Evidence | Result |
|---|---|---|
| Search → full read → feedback | The model returned the generated approval code and source URI; feedback retained the search retrieval ID | Passed |
| Explicit memory save → team promotion | Model invoked both tools; a second team member read the exact shared text | Passed |
| Private memory isolation | Another agent received an error tool message with HTTP 403 | Passed |
| Team membership isolation | An agent without team membership received HTTP 403 | Passed |
| Account isolation | The same agent ID under another account received HTTP 404 | Passed |
| Document navigation with `astream` | Model called `ls`, `stat`, `sections`, and `read_section`; answer contained the generated recovery code, duration, and source URI | Passed |
| Skill publish and version reads | Model published v2; both v1 and v2 remained readable with their exact original contents | Passed |
| Invalid API key | HTTP 401 became an error tool message; the real model explained the authentication failure and stopped | Passed |

The two generated approval codes were present only in the stored test content,
not in the user questions. The successful run exercised all ten
`contexthub_*` tools through the real Deep Agents graph, including list and object
tool responses. The three isolation probes invoked the adapter tools directly
and checked error status/content without relying on a model's interpretation.

Provider-reported usage across the five successful agent workflows was 79,291
input tokens and 2,693 output tokens (81,984 total). These counts include repeated
Deep Agents built-in instructions and tool definitions; they are not a production
cost estimate. Earlier unsuccessful attempts are excluded from these counts.

## Issue found and fixed

The first live memory creation returned HTTP 500. PostgreSQL rejected a change
event INSERT because parameter `$1` was both a UUID column value and explicitly
cast as text inside `concat`, producing `AmbiguousParameterError` (`text versus
uuid`). The same expression existed in the promotion path.

Both expressions in `MemoryService` now use `$1::uuid::text`. The SQL still
stores the same creation-event keys and values. No schema migration is needed.
The PostgreSQL collaboration regression now asserts creation-event keys, source
versions, and graph scopes for both private and promoted memories, using the
seeded team's actual write membership. The nearby memory-list unit fixture was
also updated to include the service's existing validity fields.

Validation after the fix:

- 70 adapter/SDK tests passed.
- 9 memory-service unit tests passed.
- 2 PostgreSQL regressions passed in another disposable database: creation and
  promotion with persisted events, and private-agent visibility.
- Adapter Ruff checks and formatting checks passed.
- Wheel and source-distribution builds passed; the source package includes the
  live-check example and both validation/evaluation documents.

## Environment issues and limits

The initially configured `qwen-plus` rejected chat requests with HTTP 403 because
its free quota was exhausted. The agent run switched to the already-configured
DeepSeek endpoint. No provider billing settings were changed.

The server retained its existing DashScope chat/embedding configuration. Document
ingestion therefore used its existing deterministic Markdown-heading fallback
after the Qwen chat request failed. Real document upload, storage, metadata,
section listing, section reads, and streamed agent execution passed. Successful
LLM-generated document trees have **not** been validated by this run. Embedding
requests continued to succeed.

An earlier test attempt used a team-specific document URI. The current server
requires `ctx://resources/` and root-team access; the live-check fixture and its
setup instructions were corrected to match that contract.

The successful run reused the isolated test database from earlier attempts, so
its search also encountered earlier synthetic memories; the model selected the
record matching the requested run ID. No existing research database was used.

This run did not validate production load, every model provider, custom subagent
delegation against a live model, automatic recall/capture, or SQL/text benchmarks.
The [automatic memory evaluation question](automatic-memory-evaluation.md) is
tracked separately.

## Reproduction and evidence

Run [`examples/live_check.py`](../examples/live_check.py) using the disposable
database setup and environment instructions in the [adapter README](../README.md).
It writes a structured report plus model/tool messages and synthetic fixtures.
Assertions fail with a nonzero exit status.

The original local evidence is under
`/private/tmp/contexthub-deepagents-live-20261103/`: `run-4/report.json`, the five
workflow JSON traces, `database-verification.json`, `server.log`, and
`memory-regression.log`. Earlier failed reports are retained in `run-1` through
`run-3`. These temporary files are local evidence, not repository dependencies.

The temporary services, database roles, databases, and server document store were
removed after verification. The reports and synthetic input/output traces were
retained. Credentials were loaded from local configuration without copying them
into the repository or reports.
