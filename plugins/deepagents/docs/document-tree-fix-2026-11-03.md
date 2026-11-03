# Document tree fix and live validation — 2026-11-03

Result: the previously failing nested Markdown and Chinese plain-text cases now
pass with the real configured model. Both used model-generated trees with no
fallback. The complete ten-tool Deep Agents workflow also passed.

## Changes

- Added `CHAT_ENABLE_THINKING`, forwarded to the server chat client and included
  in recorded-call request hashes when set. The current DashScope server uses
  `false`, allowing the existing 2,000-token budget to produce final output.
- Empty final chat responses raise an explicit error with the finish reason;
  failed/invalid model tree responses and the chosen sectioning path are logged.
- Replaced model-generated character offsets with verified source-line labels
  and exact copied start lines. Code computes parent links and all start/end
  positions. No old model-offset protocol is accepted.
- Removed truncated-prefix tree prompts. Documents that exceed the full-source
  prompt budget use deterministic sectioning across their complete text.
- Fixed repeated-title matching in heading fallback. A reproduced input with
  two `# Repeat` headings previously produced a zero-length first section and
  reused its position for the second. Matching now advances through whole source
  headings; unmatched reconstructed headings trigger sequential chunking rather
  than guessed positions.

The [configuration guide](../../../docs/setup/document-section-generation.md)
describes the setting and behavior. The worktree's ignored local `.env` contains
only `CHAT_ENABLE_THINKING=false` and explanatory comments. Credentials and model
names were loaded from the user's original project configuration at launch; that
original configuration was not edited. No schema migration is required.

## Real-model results

Provider: DashScope's OpenAI-compatible endpoint.
Model: `deepseek-v4-pro-0813`, the user's updated choice.
No balance/quota error occurred and no alternate model was substituted.

The final server was freshly started after the last fallback correction. Both
documents were uploaded through HTTP, processed by the real model, persisted in
PostgreSQL, and read back through the section API. The application role was
neither superuser nor allowed to bypass row-level security.

| Final case | Model tree | Source and content checks | Generation time | Reported completion tokens |
|---|---|---|---|---|
| Nested Markdown: preparation, recovery, database/application rollback | Passed; no fallback | Parent/child links, valid ranges, complete codes/durations, and no adjacent-section codes | 4.024 s | 269 |
| Chinese plain text: preparation and recovery without Markdown markers | Passed; no fallback | Expected sections, exact source slices, complete codes/durations, and no adjacent-section codes | 2.612 s | 148 |

Final case run: 2026-11-03 14:47:55–14:48:02 Hong Kong time (UTC+8), approximately
7.38 seconds including upload, embeddings, storage, and section-read assertions.
Each document required one model call and returned nonempty final JSON.

The full Deep Agents run (`13d6867afc09`) passed at 14:44:36–14:45:41 Hong Kong
time, approximately 65.44 seconds. It covered search/read/feedback, memory save
and team sharing, document navigation with `astream`, skill publication and
pinned version reads, and the 401/403/404 permission checks. All ten tools ran
through the real model. Its document also used the model tree path with no
fallback (151 completion tokens). This run preceded the additional fallback-only
correction; the final two-document run above used the complete final code.

## Regression checks

- 120 related unit tests passed, covering ingestion, retrieval, grounded
  hierarchy/source ranges, Unicode and CRLF, repeated headings, invalid anchors,
  output-budget failure reporting, chat configuration, and recorded requests.
- 10 document API/database tests passed in a separate disposable database,
  including ingestion and search through the new model response contract.
- The unit invocation skipped two DB tests that were subsequently included in
  the database run. One PDF extraction unit test was skipped because PyMuPDF was
  absent in that environment. PDF extraction itself was not changed.
- No new Ruff findings compared with HEAD in modified existing files; the new
  chat-configuration test file passes Ruff. `git diff --check` passed.

The initial combined test invocation encountered six database-connection errors
in existing document API fixtures, which use the default local DB address. The
sandbox blocked those connections before any database operation. Those tests
were then run against a dedicated temporary database and passed. No test suite
was allowed to truncate the research database.

The tests establish the observed fixes and these examples, not universal section
identification accuracy for arbitrary long documents or PDFs. The 120,000-character
prompt limit and deterministic fallbacks remain explicit behavior.

## Evidence and cleanup

Final document evidence is under
`/private/tmp/contexthub-tree-fix-final-20261103/`: `tree-check/report.json`,
the synthetic fixtures, `tree-traces/`, `server.log`, `database-tests.log`, and
`lint-delta.json`. The complete agent run and first successful section checks
are under `/private/tmp/contexthub-tree-fix-20261103/`. Unit results are retained
at `/private/tmp/contexthub-tree-fix-unit-final.log`.

The temporary observer recorded real responses and fallback selection without
substituting outputs. Final document calls were sequential, so their usage
counters did not overlap. Temporary servers, databases, roles, and server
document-store directories were removed after validation; reports and synthetic
fixtures were retained.
