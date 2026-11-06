"""Query-independent original ranges shared by lexical and vector retrieval."""
from __future__ import annotations

import re

from .result import MAX_SNIPPET_CHARS


def document_ranges(text: str, sections: list[dict], max_chars: int = MAX_SNIPPET_CHARS) -> list[tuple[int, int]]:
    """Cover the complete source once, retaining chapters and parent introductions."""
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    children: dict[int | None, list[dict]] = {}
    for section in sections:
        start, end = section["start_offset"], section["end_offset"]
        if start is not None and end is not None and 0 <= start < end <= len(text):
            children.setdefault(section["parent_id"], []).append(section)
    for siblings in children.values():
        siblings.sort(key=lambda section: (section["start_offset"], section["section_id"]))
    ranges = []

    def visit(start, end, node_id):
        nested = children.get(node_id, [])
        wrapper = start == 0 and end == len(text) and nested
        if end - start <= max_chars and not wrapper:
            if start < end:
                ranges.append((start, end))
        elif nested:
            cursor = start
            for child in nested:
                left, right = child["start_offset"], child["end_offset"]
                ranges.extend(split_range(text, cursor, left, max_chars))
                visit(left, right, child["section_id"])
                cursor = right
            ranges.extend(split_range(text, cursor, end, max_chars))
        else:
            ranges.extend(split_range(text, start, end, max_chars))

    visit(0, len(text), None)
    return ranges


def enclosing_section(sections: list[dict], start: int, end: int) -> dict | None:
    enclosed = [s for s in sections if s["start_offset"] is not None and s["end_offset"] is not None
                and s["start_offset"] <= start and s["end_offset"] >= end]
    return min(enclosed, key=lambda s: (s["end_offset"] - s["start_offset"], -s["depth"], s["section_id"]), default=None)


def embedding_input(text: str, sections: list[dict], start: int, end: int) -> str:
    section = enclosing_section(sections, start, end)
    by_id = {s["section_id"]: s for s in sections}
    titles = []
    while section is not None:
        # Source headings only; synthetic roots/reading ranges are not titles.
        if re.fullmatch(r"heading-\d+", section.get("node_id", "")):
            titles.append(section["title"])
        section = by_id.get(section["parent_id"])
    prefix = " > ".join(reversed(titles))
    return (prefix + "\n\n" if prefix else "") + text[start:end]


def split_range(
    text: str, start: int, end: int, max_chars: int, boundary_level: int = 0,
) -> list[tuple[int, int]]:
    """Pack paragraphs, then sentences, then words within one source range.

    The final character split is only for an individual run longer than the
    limit with no natural boundary. Every result remains an exact slice.
    """
    if start >= end:
        return []
    if end - start <= max_chars:
        return [(start, end)]
    boundaries = (
        r"\r?\n[ \t]*\r?\n(?:[ \t]*\r?\n)*",
        r"[.!?][\"'”’)\]]*(?:\s+|$)|[。！？][\"'”’」』）)\]]*\s*",
        r"\s+",
    )
    if boundary_level == len(boundaries):
        return [(left, min(end, left + max_chars))
                for left in range(start, end, max_chars)]

    cuts = [start] + [start + match.end() for match in re.finditer(
        boundaries[boundary_level], text[start:end],
    )] + [end]
    parts = []
    for left, right in zip(cuts, cuts[1:]):
        parts.extend(split_range(text, left, right, max_chars, boundary_level + 1))
    packed: list[tuple[int, int]] = []
    for left, right in parts:
        if packed and right - packed[-1][0] <= max_chars:
            packed[-1] = (packed[-1][0], right)
        else:
            packed.append((left, right))
    return packed
