# Document section generation: original failures and resolution

Status: the observed empty-output and incorrect-source-position defects were
fixed and verified with the real model on 2026-11-03. See the
[fix and validation report](document-tree-fix-2026-11-03.md). The investigation
below is retained as the pre-fix record; broader document-quality evaluation
remains separate.

Recorded on 2026-11-03 during the updated-model Deep Agents live check.
This concerns ContextHub's existing document-ingestion service. It is separate
from the adapter's tool transport and from automatic conversational memory.

## What a section tree does

For an uploaded document, ContextHub builds a root plus nested sections. Each
section has a title, summary, parent, and start/end character positions in the
extracted source text. Retrieval can select a section; the section-read API uses
those positions to return the original text.

Consequently, a plausible table of contents is insufficient: the character
positions must also identify the intended passage without losing facts or
including text from adjacent sections.

## Observed failures

Configuration: DashScope's OpenAI-compatible endpoint,
`CHAT_MODEL=deepseek-v4-pro-0813`; the existing server tree request uses
`max_tokens=2000` and does not set a thinking-mode parameter.

1. **Successful request, empty final text.** A real tree request returned HTTP
   200 with 2,000 completion tokens and an empty final response. The server used
   its existing Markdown-heading fallback. The model's default thinking mode
   consumes the output budget before returning the requested JSON. A separate
   request with `enable_thinking=false`, the same model, the same prompt, and the
   same output limit returned complete JSON with `finish_reason=stop` in 205
   completion tokens. This is consistent with the provider's documented
   [thinking-mode control](https://docs.bailian.console.aliyun.com/en/model-studio/deepseek-api).
2. **Valid JSON, wrong text boundaries.** That non-thinking response passed the
   server's structural validator, but its `Release` range ended at character 62
   and returned only `Release starts on`, dropping `Tuesday.`. Its `Recovery`
   range started at 63, included the preceding chapter's `Tuesday.`, and ended
   at 137, omitting the required `Pause the rollout for 23 minutes.` sentence.
   The source had 171 characters. This was checked by feeding the real response
   through the existing parser/validator and slicing the exact original text;
   it was not a second ingestion/DB run.

Two additional live ingestions confirmed the fallback behavior:

- A nested Markdown fixture retained its expected hierarchy and readable facts
  through the heading parser, but did not use a model-generated tree.
- A Chinese plain-text fixture without Markdown markers fell back to sequential
  chunks and did not expose the expected preparation/recovery sections.

The supplemental model-tree checks therefore **failed**, even though document
upload and section-read HTTP requests succeeded. Tool-level success must not be
reported as proof of model-generated section quality.

## Suggested next work

Fix this before relying on model-generated document sections in retrieval
benchmarks. It need not change the explicit Deep Agents adapter interface.

- Configure the server's provider-specific thinking/output behavior explicitly;
  changing the model name alone does not establish a usable output budget.
- Ground section boundaries in identifiable source text. Let code determine
  exact positions rather than trusting a model to count characters.
- Check expected facts and neighboring-section separation in addition to JSON
  structure, parent relationships, and numeric bounds.
- Make fallback selection observable so an acceptance run can distinguish
  model generation, heading parsing, and sequential chunking.

At the time of this initial investigation, no production thinking-mode setting
or source-position fix had been introduced. The one non-thinking request was a
diagnostic probe using the same model; the user's configuration files were not
edited. The subsequent fix is described in the report linked above.

## Evidence

The updated-model report is [here](live-validation-2026-11-03-updated-model.md).
Original local records are under
`/private/tmp/contexthub-deepagents-recheck-20261103/`: `tree-traces/`,
`tree-check/report.json`, `nonthinking-probe.json`, and
`nonthinking-span-check.json`. The temporary observer recorded real responses
and which fallback functions ran without changing their returned values.
Some initial document calls overlapped; the chat client's mutable usage counters
are not used to assign per-document costs for those overlapping calls.
