# Document section generation

ContextHub builds section metadata when a document is ingested. Section titles, parents, summaries, and character ranges are stored in PostgreSQL; section reads slice the extracted source file using those ranges.

## Model configuration

For DashScope hybrid-thinking models such as `deepseek-v4-pro-0813`, set this in the server's environment or local `.env`:

```dotenv
CHAT_ENABLE_THINKING=false
```

The server sends the provider's `enable_thinking` parameter only when this setting is supplied. Leave it unset for endpoints that do not support that parameter. This setting controls ContextHub's server-side chat calls, not the Deep Agent's own model configuration. Model names, credentials, and embedding settings remain separate configuration fields.

Tree generation has a 2,000-token output limit. Thinking can consume that budget before the provider produces a final JSON response. An empty final response raises a descriptive chat-client error, including the finish reason. Ingestion logs the failure and can use the deterministic sectioning paths described below.

## Grounding sections in the source

The model receives the original extracted text with numbered lines. It identifies section titles, levels, summaries, start-line labels, and exact copies of those start lines. Code verifies the line label and copied text, derives parent links from the levels, and computes character positions from the source itself.

Each section ends where the next section at the same or a shallower level begins. The final section ends at the actual document end. Code computes all character offsets and end positions. Repeated titles are distinguished by their source line labels. The public document/section API and database schema are unchanged; no migration is required.

## Fallbacks and limits

Invalid model anchors, malformed/empty output, or a failed model call cause fallback to heading parsing, then sequential chunks. Heading parsing also matches original headings in order and stops rather than guessing an unmatched position. It supports repeated headings and headings wrapped across lines.

If the complete numbered source would exceed the 120,000-character prompt limit, the service skips model generation and uses deterministic sectioning for the whole document. The model is never asked to infer positions in text it has not seen. Existing oversized-node splitting still applies to the resulting tree.

Enable INFO logging for `contexthub.services.document_ingester` to distinguish `source=llm`, `source=markdown_headings`, and `source=sequential_chunks`. Invalid model output is also logged as a warning. A successful ingestion alone does not show which path was used.

These checks make source boundaries deterministic for accepted anchors; they do not prove that a model identified every semantically appropriate section. Broad document-quality evaluation remains separate.
