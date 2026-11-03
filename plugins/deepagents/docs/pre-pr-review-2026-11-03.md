# Pre-PR review — 2026-11-03

The adapter, related server fixes, and tutorial passed the checks below. The
repository-wide test suite has existing failures; it is not fully green.

## Regression and packaging

| Check | Result |
|---|---|
| Default server suite | 1,000 passed, 80 skipped, 47 failed |
| `upstream/main` at `eaf901c388c57b78a6349644be44105a4f4626ea`, same environment | 974 passed, 80 skipped, 48 failed |
| New failures relative to upstream | None; one stale memory-list test fixture was corrected |
| Focused adapter, SDK, document, chat-configuration, and memory suite | 171 passed, 8 DB tests skipped |
| Separate disposable-database checks | 12 passed: 10 document API/ingestion/retrieval and 2 memory/visibility checks |
| Adapter and new chat-configuration test lint | Passed |
| Wheel and source distribution | Built successfully; `pip check` passed |
| Changed documentation links and `git diff --check` | Passed |

The 47 shared failures comprise 46 missing AgentLeak/MEME experiment artifacts
under ignored `runs/` directories and one existing admin-route registration test
failure. The baseline was extracted to a temporary directory; only the same
database opt-in guard was applied before running it. No database fixture was
allowed to truncate an existing development/research database.

The new guard applies to all tests with DB fixture dependencies, including the
previously uncovered document API tests. Database tests still require explicit
`CONTEXTHUB_INTEGRATION=1`. The focused run includes the PDF extraction test that
was skipped in the earlier, smaller environment.

## Real model and tutorial

Final acceptance run `9eea1d4270e0`: **passed**, 2026-11-03 15:14:46–15:15:48
Hong Kong time, approximately 62.18 seconds. Stack: Deep Agents 0.7.32,
LangChain 1.4.8, langchain-core 1.7.4, Python 3.13, and DashScope
`deepseek-v4-pro-0813`.

All ten tools ran against a fresh migrated PostgreSQL database using a
non-superuser application role. The run used the tutorial's seeded `acme`,
`query-agent`, `analysis-agent`, and `engineering` identities. It checked search,
read, feedback, private memory save, team promotion, document navigation and
streaming, skill publication and pinned reads, and 401/403/404 error boundaries.
Document ingestion used one real, nonempty model response with no fallback.
The final server log contained no errors. No provider quota failure occurred.

Separately, all five `query.py` prompts and the verbatim SDK upload snippet from
the [tutorial](../../../docs/setup/deepagents-integration-guide.md) passed with
the real model. SDK read-back confirmed the shared memory and document were
visible to the second agent, including `CEDAR-842` and `12 minutes`.
Docker was not running locally, so these checks used a temporary database on the
existing PostgreSQL service; the Docker startup command was not executed.

## Existing background-worker limitation

The initial tutorial run exposed a server issue with background propagation:
`PropagationEngine._fetch_dependents` accesses RLS-protected contexts without
binding `app.account_id` on its connection. A non-superuser application role
therefore encounters `unrecognized configuration parameter "app.account_id"`.
The adapter's foreground operations succeeded.

The final tutorial explicitly disables propagation and periodic lifecycle
workers, matching the earlier adapter acceptance setup. Background-worker tenant
binding needs a separate server fix; this PR does not claim to validate that
path. The README's previous assertion that RLS requires a superuser was also
corrected: a superuser bypasses RLS.

## Local evidence

- `/private/tmp/contexthub-pr-review-tests.log` and `contexthub-pr-review-baseline.log`
- `/private/tmp/contexthub-pr-review-focused.log`
- `/private/tmp/contexthub-pr-review-memory-db.log` and `contexthub-pr-review-documents-db.log`
- `/private/tmp/contexthub-pr-review-live/tutorial/report.json`
- `/private/tmp/contexthub-pr-review-final-live/acceptance/report.json` and `tree-traces/`
- `/private/tmp/contexthub-pr-artifacts/`

Temporary servers, databases, roles, and uploaded-document directories were
removed after verification. Synthetic transcripts and reports were retained.
