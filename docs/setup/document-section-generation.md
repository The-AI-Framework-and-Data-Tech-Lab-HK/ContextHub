# Document section generation

For the retrieval flow diagram, inner versus outer fusion, implementation
decisions and open questions, start with
[Document Retrieval Design](../design%20and%20development/document-retrieval.md).
This page covers generation, source boundaries, configuration and verification.

ContextHub stores one resource row per uploaded document: document L0, optional
L1, URI, version, provenance tags, and `file_path`. Original chunk vectors live separately in
`document_chunk_embeddings`, keyed by document/version and source range. L2 is read from the extracted
source file; `contexts.l2_content` is NULL. `document_sections` stores parent
links, titles, local descriptions, and character ranges. It does not copy the
document L0/L1 or complete body into each section.

## Document layers

- **L0:** the existing document-generation call now returns a document
  introduction together with anchored section descriptions. Its final L0 is
  embedded once. There is no separate summary-generation call. If generation is
  disabled, unconfigured, oversized, or fails, L0 is explicitly metadata-only:
  `Document: <source filename stem>`. It is never the first 80 body characters.
- **L1:** code renders navigation from verified source headings after their
  section IDs have been saved. Entries contain titles, hierarchy, section IDs,
  and character ranges. No model call is required. Without recognized source
  headings, L1 is NULL; sequential reading ranges are not promoted to chapters.
- **L2:** the complete extracted original remains in `extracted.txt`, alongside
  the copied source file and extracted Markdown. Document and section reads use
  the existing ACL and version checks.

Short standalone contexts may instead have NULL L0/L1 and an inline L2 original.
Their discovery embedding uses the original text. Table metadata keeps its own
schema/catalog path; it is not processed as an article. Business rows remain in
the source system.

## Source structure and model calls

The full extracted text is numbered before the existing combined generation call.
The response contains `description` and `sections`. Code checks section line
labels, exact source lines, ordering, and nesting. A valid model paragraph anchor
proves a location, not a chapter: navigation is defined by source headings, not
model-invented boundaries. Model section descriptions are reused only when title
and complete source span match the deterministic heading structure.

Recognized structure currently means Markdown headings outside fenced code
blocks, or headings reconstructed by the existing PDF extractor and reanchored
in the source. Plain text without recognized headings uses sequential ranges.
This conservative parser may miss real structure; it does not claim universal
semantic heading detection. A separate table-of-contents page is not required.
Repeated and wrapped headings are matched in source order. Original heading nodes
retain their IDs and spans; oversized leaves gain bounded child reading ranges.
Large parent sections can span multiple children.

The combined call retains the 2,000-token output budget. If the complete numbered
source exceeds 120,000 characters, generation is skipped for the entire document.
No model is asked to describe unseen tail text. NoOp/offline ingestion is allowed.
Logs report `source=markdown_headings` or `source=sequential_chunks`; these describe
structure provenance, not whether L0 generation succeeded. Malformed responses
and provider failures are logged separately.

## Original-text discovery

Normal summary/vector discovery and existing tree navigation remain available.
A separate keyword route searches all eligible file-backed resources, independent
of L0 candidate membership, vector availability, or an arbitrary document limit:

1. Apply tenant, type, scope, status and freshness filters in SQL, then ACL before
   any source file is read.
2. Tokenize query and original fragments consistently. English word/number runs
   match complete tokens; Han characters are tokenized individually for unspaced
   text. Preserve the existing query stopwords and deduplication, without stemming
   or a keyword-extraction model call.
3. Build all source ranges using the stored section tree, before filtering any
   query matches. Return a complete chapter when
   it fits the 4,000-character snippet limit; otherwise descend into its children.
   Document-wide root/title wrappers do not collapse sibling chapters into one
   snippet. Parent introductions remain searchable in their own source ranges.
4. Split oversized remaining ranges at paragraph boundaries, then sentence
   boundaries, then whitespace. Only a single unbroken run longer than the limit
   needs a hard character split. Never merge across sibling chapter boundaries.
   Score these originals with the shared BM25 computation (`k1=1.5`, `b=0.75`).
   Document frequency and average token length use all ranges of all supplied
   eligible, ACL-filtered documents, including zero-hit ranges. Only then retain
   positive-score ranges for fusion with original-vector candidates. Neither
   vector top-k nor matching-range filtering defines the BM25 statistics corpus.
5. Independently search `document_chunk_embeddings` with the existing query
   embedding, restricted to ACL-visible eligible documents and matching document
   versions. Check the source hash before reading the exact stored range. Resolve
   each hit to its original section ID.
6. Keep the union of lexical and vector candidates, keyed by document, version
   and exact range. Select `original_fusion` per search (`weighted` by default,
   or `rrf`). In weighted mode, complete cosine scores for lexical-only candidates by looking
   up their existing stored range vectors with the same version/source checks.
   This does not add candidates or call an embedding model. Because BM25 emits
   every positive match, vector-only candidates have a genuine BM25 score of zero.
   Normalize each score over the candidate union with `(score-min)/(max-min)`;
   a constant/empty route contributes no ranking distinction. Combine with the
   fixed weights `0.8 * normalized_cosine + 0.2 * normalized_bm25`. A range with
   no valid stored vector has no vector contribution and is excluded from cosine
   statistics, rather than receiving a fabricated cosine of zero.
   In RRF mode, instead sum `1/(60 + route_rank)` from the original two ranked
   discovery lists, with equal route weights. An absent route contributes zero;
   no cosine completion creates an extra vector vote in this mode.
   Retain different ranges from the same document, including ranges sharing a
   section ID. A range found by both routes is one hit. Replace that document/
   version's old discovery/tree representative with its original ranges. When
   other contexts remain, the separate outer merge retains its existing
   equal-weight RRF constant 60, keyed by document/version/range. When only
   originals remain, keep their selected inner-fusion scores without another fusion step.
7. Apply common quality factors, validity handling, ACL masks and the final exact
   version check, then take global `top_k` evidence hits. There is no one-hit-per-
   document quota. Explicit L0 discovery still returns one result per document.
   Original-only hits do not trigger extra tree-navigation calls and are not
   reranked using only their L0/L1. The existing vector candidate budget remains
   `top_k * 3`; API/SDK defaults remain 10 and the DeepAgents tool default remains 5.

The old conditional keyword fallback after tree navigation has been removed;
independent original discovery supplies that evidence. Searches restricted to
non-resource types do not scan document files. `snippet_offset` carries a half-open
character range `[start, end)` through the API, SDK and DeepAgents tool, alongside
URI, version and section ID. It locates the unmasked extracted original, even if
field masking changes the delivered text's length. Existing section reads remain
available. Candidate and materialized traces retain individual range locations.
The public materialized trace now uses the same masked text as served results.
Evidence selection reads existing source ranges; it does not rebuild stored
sections, change L0/L1, or regenerate embeddings.

The DeepAgents adapter emits one `content` body per hit. For L1 requests, a located
original snippet takes priority over navigation/description; URI, version and
section ID and source range remain available. For file-backed evidence hits,
the generic search result leaves L0/L1/L2 body fields empty instead of repeating
document layers alongside each snippet. Explicit L0 discovery retains overview
fields. Stored layers and complete document/section reads are unchanged.

Returning multiple existing ranges requires no extra model call or index
rebuild. More distinct evidence can increase the returned text; it does not by
itself improve relevance. Passage-vector storage requires migration `011`,
as described below.

## Selecting original-passage fusion

`POST /api/v1/search`, the Python SDK and the DeepAgents `contexthub_search`
tool accept the same `original_fusion` argument. The default is `weighted`.
This selects only original-document passage fusion; the outer merge with remaining
context-level results (including other resource documents) retains its existing RRF
when both lists remain after document replacement. Invalid values are rejected, and the
server retrieval trace records the selected `original_fusion` value.

| Value | Behavior |
|---|---|
| `rrf` | Equal-weight reciprocal rank fusion with rank constant 60. |
| `weighted` | Complete stored-vector scores, min/max normalize over the candidate union, then combine vector 0.8 + BM25 0.2. |

Python SDK:

```python
rrf_results = await client.search(query, top_k=5, original_fusion="rrf")
weighted_results = await client.search(query, top_k=5, original_fusion="weighted")
```

API body or DeepAgents search-tool arguments:

```json
{"query": "1986 VAX 8800", "top_k": 5, "original_fusion": "rrf"}
```

Switching the request parameter does not change the corpus, chunk ranges,
embeddings or BM25 statistics. The fixed weights and RRF constant are not
additional tuning parameters. See the
[validation summary](../design%20and%20development/document-retrieval.md#validation-and-limitations)
for automated checks, observed ranking tradeoffs and the historical sample scope.

## Limits and index handling

Min/max normalization depends on the candidate set; the fixed weights do not
guarantee preservation of every vector rank. Limited sample checks show tradeoffs
between exact-identifier and semantic queries, not a universally better policy.

The lexical route still scans eligible files: I/O grows with corpus size. The
original-vector route adds semantic candidates without a summary gate; neither
route guarantees relevance. No inverted index, generated per-chunk summary or
new model reranker is introduced. BM25 statistics are computed in memory on
search; no schema migration or vector rebuild is required. English inflections
remain distinct (`make`/`makes`, `cpu`/`cpus`); Han character tokenization is not
linguistic word segmentation. Ranking quality and large-corpus latency require
evaluation. Existing L1 reranking retains its own tokenizer and candidate-local
statistics; the shared scoring formula does not merge these routes.

Migration `011` adds version-bound chunk vectors and an HNSW index. New document
ingestion reuses source headings and natural boundaries to cover the original
once, including parent introductions, without overlapping parent/child vectors.
Embedding inputs are source title paths plus exact slices. The existing batch
client embeds them; this adds no chat calls. Configured chunk-embedding failures
abort ingestion instead of silently saving a partial index. Explicit NoOp
embedding ingestion remains supported without vectors. Source bodies and L0/L1
are not duplicated into this table.

The query-independent range builder is shared by lexical selection and indexing.
The 4,000-character body limit is not a token count; provider token limits still
apply to the title-plus-body input. Search reuses its query embedding. Existing
LLM tree navigation remains separate and can still incur its original calls.
Changing a document version invalidates older chunk rows for retrieval; indexing
the current version is explicit, with no automatic historical backfill.

Changing L0 changes its embedding input. New document introductions must be
embedded in a new isolated ingestion/index; do not reuse old prefix embeddings.
L1 display changes alone do not require new embeddings if indexed ranges and title
paths stay unchanged. Changed range or title-plus-body inputs require corresponding
new chunk embeddings.
The new chunk-table migration does not backfill data or rebuild old L0 vectors.
Existing indexes are not changed automatically. Apply migration `011` before
running the updated service and explicitly index existing documents if passage
vectors are needed.

## Model configuration

`EMBEDDING_MODEL` selects the embedding model independently of `CHAT_MODEL`.
`EMBEDDING_DIMENSIONS` is sent as the provider's `dimensions` parameter and is
also the expected response width. A mismatched response is rejected, not padded.
The model must support that requested width, and the database vector column must
match it. Changing the model or effective dimensions requires a separate,
consistently embedded index; equal vector widths do not make different model
spaces compatible. A configuration change does not automatically regenerate
existing embeddings.


For a supported hybrid-thinking endpoint, `CHAT_ENABLE_THINKING=false` can leave
the output budget available for final JSON. The parameter is sent only when
configured. Credentials, model names, and embedding settings remain separate.
A successful fallback ingestion does not prove model-generation quality.

## Verification

The [validation summary](../design%20and%20development/document-retrieval.md#validation-and-limitations)
provides a self-contained regression command and distinguishes automated checks
from historical source/provider checks. Complete experiment records remain in a
separate archive; no benchmark artifacts are needed by the maintained tests.

The [manual checks](../../scripts/task6_manual_verification.md#verify-model-generation-and-source-boundaries)
and [Deep Agents tutorial](deepagents-integration-guide.md#6-upload-and-read-a-document)
exercise document and section reads. Compare character slices with `extracted.txt`,
not PDF page positions or byte offsets.
