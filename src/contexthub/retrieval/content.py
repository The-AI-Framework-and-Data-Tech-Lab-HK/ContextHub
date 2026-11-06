"""Content selection for optional summaries and raw-only contexts.

The existing l0_embedding column stores one discovery vector. When summaries
are absent, index the original text; never manufacture summaries by copying it.
"""

EMBEDDING_TEXT_SQL = "COALESCE(NULLIF(l0_content, ''), NULLIF(l1_content, ''), NULLIF(l2_content, ''))"
RAW_CONTENT_SQL = """CASE
    WHEN NULLIF(l0_content, '') IS NULL AND NULLIF(l1_content, '') IS NULL
         AND file_path IS NULL THEN l2_content
    END"""


def embedding_text(context) -> str | None:
    return context.get("l0_content") or context.get("l1_content") or context.get("l2_content")
