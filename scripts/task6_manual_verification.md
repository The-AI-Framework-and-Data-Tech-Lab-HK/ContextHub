# Long-document ingestion and retrieval verification (Task 6)

This document explains how to manually verify the long-document retrieval flow after configuring the server models in `ContextHub/.env`. For how section boundaries are generated and validated, see [document section generation](../docs/setup/document-section-generation.md). To verify the same document operations through an agent, use the [Deep Agents tutorial](../docs/setup/deepagents-integration-guide.md).

The goal is to confirm that ContextHub can:

1. ingest a long document,
2. build `document_sections`,
3. run `RetrievalService.search()`,
4. return the ingested document with:
   - non-empty `snippet`
   - `retrieval_strategy == "tree"` or `retrieval_strategy == "keyword"` when fallback activates
   - non-empty `section_id` (required by the current smoke script for both strategies)


## What you need

Before running the manual smoke script, make sure you have:

1. PostgreSQL running
2. database migrations applied
3. a valid LLM setup in `ContextHub/.env`

You do not need to start the HTTP server: the script starts the FastAPI application lifespan in-process. It adds root-team write membership for the selected agent, ingests a document, and leaves database records and document files for inspection. Use a disposable demo database; the default `acme` account and `query-agent` match the seeded root-team ID used by the script. For an ordinary database account, configure the background-worker flags described in the [Deep Agents tutorial](../docs/setup/deepagents-integration-guide.md).


## Minimum setup

From the `ContextHub` repo root:

```bash
alembic upgrade head
```

Check that your `.env` contains a valid key. The defaults are:

```bash
OPENAI_API_KEY=your_real_key_here
OPENAI_BASE_URL=https://api.openai.com/v1
CHAT_MODEL=gpt-4o-mini
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIMENSIONS=1536
```

What these defaults mean:

- `CHAT_MODEL` is used for long-document tree construction and tree-based section picking.
- `EMBEDDING_MODEL` is used for retrieval embeddings.
- `OPENAI_BASE_URL` can be changed if you want to target an OpenAI-compatible endpoint instead of the official OpenAI API.
- `EMBEDDING_DIMENSIONS` is the stored vector size used by ContextHub. Exact matches are accepted directly; if your provider returns a shorter compatible embedding, ContextHub pads it with trailing zeros before writing/querying pgvector.

For another compatible endpoint, replace the base URL, key, and both model names together. For endpoints that support `enable_thinking`, `CHAT_ENABLE_THINKING=false` can keep thinking from consuming the short output budget. Leave it unset when unsupported. These settings belong to the ContextHub server, independently of the Deep Agent's model.


## Fastest path

If you just want the shortest verification path, run:

```bash
python scripts/manual_longdoc_smoke.py
```

This uses an auto-generated sample markdown document and an auto-derived search query.

This is the easiest way to validate Task 6 without preparing your own file or question.


## Using your own file

If you want to validate against a real document, you can pass:

```bash
python scripts/manual_longdoc_smoke.py --source /absolute/path/to/your_file.pdf
```

Supported source formats:

- `.pdf`
- `.md`
- `.txt`

You can also provide your own URI:

```bash
python scripts/manual_longdoc_smoke.py \
  --source /absolute/path/to/your_file.pdf \
  --uri ctx://resources/manuals/my-doc
```


## Using your own query

If you want to test a specific query:

```bash
python scripts/manual_longdoc_smoke.py \
  --source /absolute/path/to/your_file.pdf \
  --query "postgres replication wal lag"
```

If you do not pass `--query`, the script will derive a discovery-friendly query from the ingested `l0_content` and `l1_content`.


## Recommended validation order

### Option A: No custom inputs

Run:

```bash
python scripts/manual_longdoc_smoke.py
```

Use this first.

### Option B: Real document, auto query

Run:

```bash
python scripts/manual_longdoc_smoke.py --source /absolute/path/to/document.pdf
```

Use this after Option A succeeds.

### Option C: Real document, custom query

Run:

```bash
python scripts/manual_longdoc_smoke.py \
  --source /absolute/path/to/document.pdf \
  --query "your explicit question or keyword query"
```

Use this only if you want to validate a specific retrieval behavior.


## What success looks like

The script should print all of the following stages:

1. `Ingest succeeded`
2. `Context row`
3. `Readback`
4. `Document sections`
5. `Search smoke`
6. `Matched search result`
7. `Manual Task 6 smoke test completed successfully.`

The most important fields in the final search result are:

- `retrieval_strategy:` usually `tree`, but `keyword` is also acceptable when tree falls back
- `section_id:` non-empty; the current script rejects a missing ID for either strategy
- `snippet preview:` non-empty


## What the script is verifying

The script now verifies the Task 6 runtime path end to end:

1. `LongDocumentIngester.ingest(...)`
2. `document_sections` written to DB
3. `ContextStore.read(...)` for `L0`, `L1`, `L2`
4. `RetrievalService.search(...)`
5. long-doc precision routing through the default `tree` strategy, with optional `keyword` fallback for low-confidence tree snippets
6. final result includes:
   - `snippet`
   - `section_id`
   - `retrieval_strategy == "tree"` or `retrieval_strategy == "keyword"`


## Verify model generation and source boundaries

The smoke script checks ingestion and retrieval, but does not compare every section against the source. `retrieval_strategy: tree` describes how the search selected a section; the section tree itself could have come from a deterministic fallback.

To see which generation path was used, run the sample with INFO logging:

```bash
python - <<'PY'
import logging
import runpy

logging.basicConfig(level=logging.INFO)
runpy.run_path("scripts/manual_longdoc_smoke.py", run_name="__main__")
PY
```

Look for `Document section tree generated: source=llm`. `source=markdown_headings` or `source=sequential_chunks` means deterministic sectioning was used. Invalid anchors or empty model output produce a warning before fallback. A numbered prompt over 120,000 characters skips model generation entirely.

Inspect a document with repeated headings, nested sections, and Chinese or other non-ASCII text. Compare the reported `start_offset` and `end_offset` against the stored `extracted.txt`, not against PDF page positions or file bytes:

1. Read the extracted text as UTF-8, preserving newlines (`open(path, encoding="utf-8", newline="")`).
2. Slice `text[start_offset:end_offset]`. These are character indices, with an exclusive end.
3. For sections anchored to headings, check that the slice starts at the intended heading and repeated headings select the right occurrence. Check that each parent's range contains its children. Generated chunk/part nodes may start within body text.
4. With the server running, use `client.document.sections(context_id)` and `client.document.read_section(context_id, section_id)` to compare returned section content with that slice. Deep Agents exposes these operations as `contexthub_sections` and `contexthub_read_section`.

To separately check the deterministic path:

```bash
python scripts/manual_longdoc_smoke.py --disable-llm-tree
```

This disables model-based tree construction only; summary generation, embeddings, and retrieval can still call models. A successful fallback does not validate model-generated sections or overall answer quality.

## Common failures

### 1. Missing or invalid LLM configuration

Typical symptom:

- ingestion fails before the long-doc pipeline completes
- or LLM-backed calls fail during ingestion/tree selection

Check:

- `ContextHub/.env` exists
- `OPENAI_API_KEY` is set correctly
- `OPENAI_BASE_URL` points to a reachable OpenAI-compatible endpoint
- `CHAT_MODEL` and `EMBEDDING_MODEL` are valid for that endpoint
- `EMBEDDING_DIMENSIONS` is large enough for the chosen embedding model output

An `empty content` error with a length-related finish reason means the provider returned no final answer within the output budget. Check the supported thinking setting and model availability; inspect generation logs even if ingestion succeeds through fallback. If the provider reports exhausted credit or quota, update the model configuration before retrying.


### 2. PostgreSQL not running

Typical symptom:

- startup fails
- DB connection errors appear immediately

Check:

- PostgreSQL is running
- your local DB config matches the project setup


### 3. Search does not return the ingested document

Typical symptom:

- the script raises:
  `Search did not return the ingested long document`

Why this usually happens:

- the discovery layer did not recall the document strongly enough
- your custom query does not match what was stored in `l0_content` / `l1_content`

What to do:

1. first try the script without `--query`
2. if using your own query, make it more explicit
3. choose terms that likely appear in the generated summary, not only in deep body text


### 4. Search returns the document but no `snippet`

Typical symptom:

- the script raises:
  `Search returned the document but snippet is empty`

Why this matters:

- this means Task 6 precision retrieval did not succeed cleanly

Possible causes:

- `document_sections` missing or malformed
- `extracted.txt` could not be read
- tree selection degraded unexpectedly


### 5. Unsupported retrieval strategy

Typical symptom:

- the script raises:
  `Expected retrieval_strategy to be 'tree' or 'keyword'`

Why this matters:

- default runtime behavior should try `tree` first

If the final result uses `keyword`, tree retrieval fell back to keyword selection. The current script also requires a non-empty snippet and section ID. Check the generation logs separately to determine how the tree was built.


## Suggested first run

After your LLM configuration is ready, this is the recommended first command:

```bash
python scripts/manual_longdoc_smoke.py
```

If that succeeds, then try:

```bash
python scripts/manual_longdoc_smoke.py --source /absolute/path/to/your.pdf
```


## Optional next step

For HTTP-level verification, start the server and call `POST /api/v1/search`, then the document section endpoints listed in the [developer guide](../docs/design%20and%20development/development-guide.md#api-overview). For agent-level validation, use the [Deep Agents live check](../plugins/deepagents/README.md#live-acceptance-check); it tests tool calls but does not establish section-boundary accuracy by itself.
