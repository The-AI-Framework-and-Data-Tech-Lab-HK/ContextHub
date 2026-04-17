# Task 6 Manual Verification

This document explains how to manually verify the Task 6 long-document retrieval flow after you have a valid `OPENAI_API_KEY`.

The goal is to confirm that ContextHub can:

1. ingest a long document,
2. build `document_sections`,
3. run `RetrievalService.search()`,
4. return the ingested document with:
   - non-empty `snippet`
   - `retrieval_strategy == "tree"`
   - non-empty `section_id`


## What you need

Before running the manual smoke script, make sure you have:

1. PostgreSQL running
2. database migrations applied
3. a valid `OPENAI_API_KEY` in `ContextHub/.env`

You do not need to start the HTTP server for this verification.


## Minimum setup

From the `ContextHub` repo root:

```bash
alembic upgrade head
```

Check that your `.env` contains a valid key:

```bash
OPENAI_API_KEY=your_real_key_here
```


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

- `retrieval_strategy: tree`
- `section_id:` non-empty
- `snippet preview:` non-empty


## What the script is verifying

The script now verifies the Task 6 runtime path end to end:

1. `LongDocumentIngester.ingest(...)`
2. `document_sections` written to DB
3. `ContextStore.read(...)` for `L0`, `L1`, `L2`
4. `RetrievalService.search(...)`
5. long-doc precision routing through the default `tree` strategy
6. final result includes:
   - `snippet`
   - `section_id`
   - `retrieval_strategy == "tree"`


## Common failures

### 1. Missing or invalid API key

Typical symptom:

- ingestion fails before the long-doc pipeline completes
- or OpenAI-backed calls fail during ingestion/tree selection

Check:

- `ContextHub/.env` exists
- `OPENAI_API_KEY` is set correctly


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


### 5. `retrieval_strategy` is not `tree`

Typical symptom:

- the script raises:
  `Expected retrieval_strategy='tree'`

Why this matters:

- default runtime behavior for Task 6 must use `tree`


## Suggested first run

After you get your API key, this is the recommended first command:

```bash
python scripts/manual_longdoc_smoke.py
```

If that succeeds, then try:

```bash
python scripts/manual_longdoc_smoke.py --source /absolute/path/to/your.pdf
```


## Optional next step

If you later want a more production-like manual check, add an HTTP-level verification using the `/search` endpoint after starting the server. That is not required for the Task 6 smoke script itself.
