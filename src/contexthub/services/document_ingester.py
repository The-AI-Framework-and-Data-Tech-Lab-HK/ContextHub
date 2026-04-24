"""Long document ingestion: filesystem + extraction + L0/L1 + section tree."""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from collections import Counter
from pathlib import Path
import re
from typing import Any

import asyncpg

from contexthub.db.repository import ScopedRepo
from contexthub.errors import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    ServiceUnavailableError,
)
from contexthub.generation.base import ContentGenerator
from contexthub.llm.base import EmbeddingClient
from contexthub.llm.chat_client import BaseChatClient, NoOpChatClient
from contexthub.models.context import Scope
from contexthub.models.document import DocumentIngestResponse, SectionNode
from contexthub.models.request import RequestContext
from contexthub.services.acl_service import ACLService
from contexthub.services.audit_service import AuditService

logger = logging.getLogger(__name__)

TREE_PROMPT_CHAR_LIMIT = 120_000

_LLM_TREE_PROMPT_PREFIX = """You are building a section tree for a long document.
Return exactly one JSON object with one top-level key named "sections".
"sections" must be a flat array.
Each array item must contain exactly these keys:
- "node_id" (string)
- "parent_node_id" (string or null)
- "title" (string)
- "start_offset" (integer)
- "end_offset" (integer)
- "summary" (string)
Rules:
- Offsets are 0-based character offsets into the plain text version of the document.
- node_id values must be unique.
- There must be exactly one logical root. If unsure, create a single root plus children.
- Do not return markdown fences.
- Do not return any explanation.

Document excerpt:
"""

_SECTION_KEYS = {
    "node_id",
    "parent_node_id",
    "title",
    "start_offset",
    "end_offset",
    "summary",
}

_PDF_STANDALONE_HEADINGS = {
    "abstract",
    "acknowledgements",
    "references",
    "appendix",
}


def doc_dir_key(account_id: str, uri: str) -> str:
    return hashlib.sha256(f"{account_id}\x00{uri}".encode()).hexdigest()[:16]


def build_bounded_tree_prompt(extracted_md: str, plain_text: str) -> str:
    budget = max(0, TREE_PROMPT_CHAR_LIMIT - len(_LLM_TREE_PROMPT_PREFIX))
    structure_excerpt = _collect_heading_excerpt(extracted_md, budget * 2 // 3)
    parts: list[str] = []
    used = 0

    if structure_excerpt:
        block = "## Candidate headings / TOC\n" + structure_excerpt
        if len(block) > budget:
            block = block[:budget]
        parts.append(block)
        used += len(block)

    remaining = max(0, budget - used - (2 if parts else 0))
    if remaining > 0:
        block = "## Leading excerpt\n" + plain_text[: max(0, remaining - len("## Leading excerpt\n"))]
        parts.append(block[:remaining])

    prompt = _LLM_TREE_PROMPT_PREFIX + "\n\n".join(parts)
    return prompt[:TREE_PROMPT_CHAR_LIMIT]


def _collect_heading_excerpt(extracted_md: str, budget: int) -> str:
    if budget <= 0:
        return ""

    collected: list[str] = []
    used = 0
    for line in extracted_md.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        looks_like_heading = bool(re.match(r"^\s{0,3}#{1,6}\s+\S", line))
        looks_like_toc = (
            "table of contents" in stripped.lower()
            or stripped.lower() == "contents"
            or bool(re.match(r"^\d+(\.\d+)*\s+\S", stripped))
        )
        if not (looks_like_heading or looks_like_toc):
            continue
        addition = stripped[:400]
        extra = len(addition) + (1 if collected else 0)
        if used + extra > budget:
            break
        collected.append(addition)
        used += extra
    return "\n".join(collected)


def _is_page_marker(line: str) -> bool:
    return bool(re.fullmatch(r"(?:page\s+)?\d+", line.strip(), re.IGNORECASE))


def _looks_like_running_header(line: str) -> bool:
    stripped = " ".join(line.split())
    if len(stripped) < 12 or len(stripped) > 120:
        return False
    if _is_page_marker(stripped):
        return True
    if stripped.endswith((".", "?", "!", ":")):
        return False
    letters = [char for char in stripped if char.isalpha()]
    if len(letters) < 6:
        return False
    upper_ratio = sum(1 for char in letters if char.isupper()) / len(letters)
    return upper_ratio >= 0.7 or "conference paper" in stripped.lower()


def _looks_like_all_caps_heading(line: str) -> bool:
    stripped = " ".join(line.split())
    letters = [char for char in stripped if char.isalpha()]
    if len(letters) < 4 or len(stripped) > 120:
        return False
    if stripped.endswith((".", "?", "!")):
        return False
    upper_ratio = sum(1 for char in letters if char.isupper()) / len(letters)
    word_count = len(stripped.split())
    if upper_ratio < 0.8:
        return False
    if word_count == 1 and stripped.lower() not in _PDF_STANDALONE_HEADINGS and len(stripped) < 8:
        return False
    return True


def _looks_like_numbered_heading(line: str) -> bool:
    stripped = " ".join(line.split())
    match = re.match(r"^(?P<prefix>\d+(?:\.\d+)*|[A-Z])\s+(?P<body>.+)$", stripped)
    if match is None:
        return False
    body = match.group("body").strip()
    if not body:
        return False
    if re.match(r"^[A-Z][A-Z0-9 ,()'/-]+$", body):
        return True
    body_letters = [char for char in body if char.isalpha()]
    if len(body_letters) < 4:
        return False
    upper_ratio = sum(1 for char in body_letters if char.isupper()) / len(body_letters)
    return upper_ratio >= 0.8


def _heading_level(title: str) -> int:
    stripped = " ".join(title.split())
    if re.match(r"^\d+\.\d+\s+", stripped):
        return 3
    if re.match(r"^\d+\s+", stripped):
        return 2
    if re.match(r"^[A-Z]\s+[A-Z]", stripped):
        return 2
    if stripped.lower() in _PDF_STANDALONE_HEADINGS:
        return 2
    return 2


def _pdf_to_markdownish_text(plain_text: str) -> str:
    raw_lines = [line.rstrip() for line in plain_text.splitlines()]
    normalized_lines = [" ".join(line.split()) for line in raw_lines]
    line_counts = Counter(
        line for line in normalized_lines if line and not _is_page_marker(line)
    )
    repeated_noise = {
        line for line, count in line_counts.items()
        if count >= 3 and _looks_like_running_header(line)
    }

    md_lines: list[str] = []
    non_empty_seen = 0
    i = 0
    while i < len(normalized_lines):
        line = normalized_lines[i]
        if not line:
            if md_lines and md_lines[-1] != "":
                md_lines.append("")
            i += 1
            continue
        if line in repeated_noise or _is_page_marker(line):
            i += 1
            continue

        if (
            re.fullmatch(r"\d+(?:\.\d+)*", line)
            and i + 1 < len(normalized_lines)
            and _looks_like_all_caps_heading(normalized_lines[i + 1])
        ):
            heading = f"{line} {normalized_lines[i + 1]}"
            md_lines.append(f"{'#' * _heading_level(heading)} {heading}")
            i += 2
            non_empty_seen += 1
            continue

        if (
            non_empty_seen <= 5
            and _looks_like_all_caps_heading(line)
        ):
            title_lines = [line]
            j = i + 1
            while j < len(normalized_lines) and _looks_like_all_caps_heading(normalized_lines[j]):
                title_lines.append(normalized_lines[j])
                j += 1
            if len(title_lines) >= 2:
                md_lines.append(f"# {' '.join(title_lines)}")
                i = j
                non_empty_seen += len(title_lines)
                continue

        if _looks_like_all_caps_heading(line) or _looks_like_numbered_heading(line):
            md_lines.append(f"{'#' * _heading_level(line)} {line}")
        else:
            md_lines.append(line)
        i += 1
        non_empty_seen += 1

    while md_lines and md_lines[-1] == "":
        md_lines.pop()
    return "\n".join(md_lines).strip()


def _extract_json_object(raw: str) -> str | None:
    text = raw.strip()
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        text = text[start : end + 1]
    return text


def parse_llm_sections_json(raw: str) -> dict[str, Any] | None:
    blob = _extract_json_object(raw)
    if not blob:
        return None
    try:
        payload = json.loads(blob)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def validate_flat_sections(payload: dict[str, Any], text_len: int) -> list[dict[str, Any]] | None:
    sections = payload.get("sections")
    if not isinstance(sections, list) or not sections:
        return None

    flat: list[dict[str, Any]] = []
    ids: set[str] = set()
    for item in sections:
        if not isinstance(item, dict) or set(item.keys()) != _SECTION_KEYS:
            return None
        try:
            node_id = str(item["node_id"]).strip()
            title = str(item["title"]).strip()
            summary = str(item["summary"]).strip()
            start_offset = int(item["start_offset"])
            end_offset = int(item["end_offset"])
        except (TypeError, ValueError):
            return None

        parent_node_id = item["parent_node_id"]
        if parent_node_id is not None:
            if not isinstance(parent_node_id, str):
                return None
            parent_node_id = parent_node_id.strip()
            if not parent_node_id:
                return None

        if not node_id or not title or not summary or node_id in ids:
            return None
        ids.add(node_id)

        start_offset = max(0, min(start_offset, text_len))
        end_offset = max(0, min(end_offset, text_len))
        if start_offset > end_offset:
            start_offset, end_offset = end_offset, start_offset

        flat.append(
            {
                "node_id": node_id,
                "parent_node_id": parent_node_id,
                "title": title,
                "start_offset": start_offset,
                "end_offset": end_offset,
                "summary": summary,
            }
        )

    roots = [node for node in flat if node["parent_node_id"] is None]
    if not roots:
        return None
    if len(roots) > 1:
        flat = _converge_roots(flat, text_len)
        if flat is None:
            return None

    parent_of = {node["node_id"]: node["parent_node_id"] for node in flat}
    all_ids = set(parent_of)
    for parent_id in parent_of.values():
        if parent_id is not None and parent_id not in all_ids:
            return None
    if _has_cycle(parent_of):
        return None

    roots = [node for node in flat if node["parent_node_id"] is None]
    if len(roots) != 1:
        return None

    root = roots[0]
    root["start_offset"] = 0
    root["end_offset"] = text_len

    by_id = {node["node_id"]: node for node in flat}
    changed = True
    while changed:
        changed = False
        for node in flat:
            parent_id = node["parent_node_id"]
            if parent_id is None:
                continue
            parent = by_id.get(parent_id)
            if parent is None:
                return None
            if node["start_offset"] < parent["start_offset"]:
                parent["start_offset"] = node["start_offset"]
                changed = True
            if node["end_offset"] > parent["end_offset"]:
                parent["end_offset"] = node["end_offset"]
                changed = True

    for node in flat:
        parent_id = node["parent_node_id"]
        if parent_id is None:
            continue
        parent = by_id.get(parent_id)
        if parent is None:
            return None
        if node["start_offset"] < parent["start_offset"]:
            return None
        if node["end_offset"] > parent["end_offset"]:
            return None

    return flat


def _converge_roots(flat: list[dict[str, Any]], text_len: int) -> list[dict[str, Any]] | None:
    synthetic_root = "__document_root__"
    if any(node["node_id"] == synthetic_root for node in flat):
        return None
    converged = [
        {
            "node_id": synthetic_root,
            "parent_node_id": None,
            "title": "Document",
            "start_offset": 0,
            "end_offset": text_len,
            "summary": "Document overview",
        }
    ]
    for node in flat:
        item = dict(node)
        if item["parent_node_id"] is None:
            item["parent_node_id"] = synthetic_root
        converged.append(item)
    return converged


def _has_cycle(parent_of: dict[str, str | None]) -> bool:
    for start in parent_of:
        seen: set[str] = set()
        current: str | None = start
        while current is not None:
            if current in seen:
                return True
            seen.add(current)
            current = parent_of.get(current)
    return False


def markdown_heading_fallback(extracted_md: str, plain_text: str, extracted_txt: str) -> list[dict[str, Any]]:
    source = extracted_md if extracted_md.strip() else plain_text
    headings = list(re.finditer(r"(?m)^(\s{0,3})(#{1,6})\s+(.+)$", source))
    if not headings:
        return []

    text_len = len(extracted_txt)
    flat: list[dict[str, Any]] = [
        {
            "node_id": "heading-root",
            "parent_node_id": None,
            "title": "Document",
            "start_offset": 0,
            "end_offset": text_len,
            "summary": _summary_from_text(extracted_txt[:400], fallback="Document overview"),
        }
    ]
    stack: list[tuple[int, str]] = []
    search_start = 0
    for index, match in enumerate(headings, start=1):
        level = len(match.group(2))
        title = match.group(3).strip()
        if not title:
            continue

        while stack and stack[-1][0] >= level:
            stack.pop()
        parent_id = stack[-1][1] if stack else "heading-root"
        node_id = f"heading-{index:04d}"

        snippet = title[:80]
        start_offset = extracted_txt.find(snippet, search_start)
        if start_offset < 0:
            start_offset = extracted_txt.find(snippet)
        if start_offset < 0:
            start_offset = min(match.start(), text_len)
        search_start = max(search_start, start_offset)

        if index < len(headings):
            next_title = headings[index].group(3).strip()[:80]
            next_offset = extracted_txt.find(next_title, search_start)
            end_offset = next_offset if next_offset >= 0 else text_len
        else:
            end_offset = text_len

        segment = extracted_txt[start_offset:end_offset]
        flat.append(
            {
                "node_id": node_id,
                "parent_node_id": parent_id,
                "title": title[:200],
                "start_offset": start_offset,
                "end_offset": max(start_offset, end_offset),
                "summary": _summary_from_text(segment, fallback=title),
            }
        )
        stack.append((level, node_id))

    return flat


def sequential_chunk_fallback(extracted_txt: str, max_token_per_node: int) -> list[dict[str, Any]]:
    text_len = len(extracted_txt)
    max_chars = max(256, max_token_per_node * 4)
    flat: list[dict[str, Any]] = [
        {
            "node_id": "seq-root",
            "parent_node_id": None,
            "title": "Document",
            "start_offset": 0,
            "end_offset": text_len,
            "summary": _summary_from_text(extracted_txt[:400], fallback="Document overview"),
        }
    ]
    if text_len == 0:
        return flat

    cursor = 0
    part_index = 0
    while cursor < text_len:
        target_end = min(text_len, cursor + max_chars)
        end = _choose_split_point(extracted_txt, cursor, target_end)
        if end <= cursor:
            end = target_end
        part_index += 1
        segment = extracted_txt[cursor:end]
        flat.append(
            {
                "node_id": f"seq-{part_index:04d}",
                "parent_node_id": "seq-root",
                "title": f"Section {part_index}",
                "start_offset": cursor,
                "end_offset": end,
                "summary": _summary_from_text(segment, fallback=f"Section {part_index}"),
            }
        )
        cursor = end
    return flat


def _choose_split_point(text: str, start: int, target_end: int) -> int:
    if target_end >= len(text):
        return len(text)
    for needle in ("\n\n", "\n", ". "):
        idx = text.rfind(needle, start, target_end)
        if idx > start:
            return idx + len(needle)
    return target_end


def flat_to_section_tree(flat: list[dict[str, Any]], extracted_txt: str) -> SectionNode | None:
    by_id: dict[str, SectionNode] = {}
    for node in flat:
        by_id[node["node_id"]] = SectionNode(
            node_id=node["node_id"],
            parent_node_id=node["parent_node_id"],
            title=node["title"],
            start_offset=node["start_offset"],
            end_offset=node["end_offset"],
            summary=node["summary"],
        )

    roots: list[SectionNode] = []
    for node in flat:
        current = by_id[node["node_id"]]
        parent_id = node["parent_node_id"]
        if parent_id is None:
            roots.append(current)
        else:
            parent = by_id.get(parent_id)
            if parent is None:
                return None
            parent.children.append(current)

    if len(roots) != 1:
        return None

    def assign(node: SectionNode, depth: int) -> None:
        node.depth = depth
        node.children.sort(key=lambda item: ((item.start_offset or 0), item.node_id))
        start = max(0, min(node.start_offset or 0, len(extracted_txt)))
        end = max(0, min(node.end_offset or 0, len(extracted_txt)))
        if start > end:
            start, end = end, start
        node.start_offset = start
        node.end_offset = end
        span = extracted_txt[start:end]
        node.summary = (node.summary or "").strip() or _summary_from_text(span, fallback=node.title)
        node.token_count = max(1, len(span) // 4)
        for child in node.children:
            assign(child, depth + 1)

    assign(roots[0], 0)
    return roots[0]


def split_oversized_nodes(root: SectionNode, extracted_txt: str, max_token_per_node: int) -> None:
    counter = 0
    max_chars = max(1, max_token_per_node * 4)

    def measure(node: SectionNode) -> int:
        start = max(0, min(node.start_offset or 0, len(extracted_txt)))
        end = max(0, min(node.end_offset or 0, len(extracted_txt)))
        if start > end:
            start, end = end, start
        node.start_offset = start
        node.end_offset = end
        span = extracted_txt[start:end]
        node.token_count = max(1, len(span) // 4)
        node.summary = (node.summary or "").strip() or _summary_from_text(span, fallback=node.title)
        return node.token_count

    def split_leaf(node: SectionNode) -> list[SectionNode]:
        nonlocal counter

        start = max(0, min(node.start_offset or 0, len(extracted_txt)))
        end = max(0, min(node.end_offset or 0, len(extracted_txt)))
        if start > end:
            start, end = end, start
        if end - start <= max_chars:
            measure(node)
            return [node]

        split_at = _choose_split_point(
            extracted_txt,
            start,
            min(end, start + (end - start) // 2 + max_chars // 8),
        )
        if split_at <= start or split_at >= end:
            split_at = start + (end - start) // 2
        if split_at <= start or split_at >= end:
            return [node]

        counter += 1
        left_text = extracted_txt[start:split_at]
        right_text = extracted_txt[split_at:end]
        parts = [
            SectionNode(
                node_id=f"{node.node_id}-part-{counter}a",
                parent_node_id=node.parent_node_id,
                title=f"{node.title} (part 1)",
                depth=node.depth,
                start_offset=start,
                end_offset=split_at,
                summary=_summary_from_text(left_text, fallback=f"{node.title} part 1"),
                token_count=max(1, len(left_text) // 4),
            ),
            SectionNode(
                node_id=f"{node.node_id}-part-{counter}b",
                parent_node_id=node.parent_node_id,
                title=f"{node.title} (part 2)",
                depth=node.depth,
                start_offset=split_at,
                end_offset=end,
                summary=_summary_from_text(right_text, fallback=f"{node.title} part 2"),
                token_count=max(1, len(right_text) // 4),
            ),
        ]

        normalized: list[SectionNode] = []
        for part in parts:
            normalized.extend(split_leaf(part))
        return normalized

    def split_internal(node: SectionNode) -> list[SectionNode]:
        nonlocal counter

        children = sorted(node.children, key=lambda item: ((item.start_offset or 0), item.node_id))
        if len(children) <= 1:
            measure(node)
            return [node]

        groups: list[list[SectionNode]] = []
        current_group: list[SectionNode] = []
        group_start = 0
        group_end = 0

        for child in children:
            child_start = child.start_offset or 0
            child_end = child.end_offset or child_start
            if not current_group:
                current_group = [child]
                group_start = child_start
                group_end = child_end
                continue

            proposed_end = max(group_end, child_end)
            if proposed_end - group_start > max_chars:
                groups.append(current_group)
                current_group = [child]
                group_start = child_start
                group_end = child_end
            else:
                current_group.append(child)
                group_end = proposed_end

        if current_group:
            groups.append(current_group)
        if len(groups) <= 1:
            measure(node)
            return [node]

        parts: list[SectionNode] = []
        for index, group in enumerate(groups, start=1):
            counter += 1
            part_start = min(child.start_offset or 0 for child in group)
            part_end = max(child.end_offset or part_start for child in group)
            part_text = extracted_txt[part_start:part_end]
            part = SectionNode(
                node_id=f"{node.node_id}-part-{counter}",
                parent_node_id=node.parent_node_id,
                title=f"{node.title} (part {index})",
                depth=node.depth,
                start_offset=part_start,
                end_offset=part_end,
                summary=_summary_from_text(part_text, fallback=f"{node.title} part {index}"),
                token_count=max(1, len(part_text) // 4),
                children=group,
            )
            for child in group:
                child.parent_node_id = part.node_id
            parts.append(part)
        return parts

    def normalize_node(node: SectionNode, *, is_root: bool = False) -> list[SectionNode]:
        normalized_children: list[SectionNode] = []
        for child in node.children:
            normalized_children.extend(normalize_node(child))
        node.children = normalized_children
        node.children.sort(key=lambda item: ((item.start_offset or 0), item.node_id))

        if is_root:
            measure(node)
            if not node.children and (node.token_count or 0) > max_token_per_node:
                root_leaf = SectionNode(
                    node_id=f"{node.node_id}-content",
                    parent_node_id=node.node_id,
                    title=node.title,
                    depth=node.depth + 1,
                    start_offset=node.start_offset,
                    end_offset=node.end_offset,
                    summary=node.summary,
                    token_count=node.token_count,
                )
                node.children = split_leaf(root_leaf)
                node.children.sort(key=lambda item: ((item.start_offset or 0), item.node_id))
            return [node]

        if (measure(node) or 0) <= max_token_per_node:
            return [node]
        if not node.children:
            return split_leaf(node)

        regrouped: list[SectionNode] = []
        for part in split_internal(node):
            if part is node:
                regrouped.append(part)
            else:
                regrouped.extend(normalize_node(part))
        return regrouped

    normalize_node(root, is_root=True)


def _summary_from_text(text: str, *, fallback: str) -> str:
    collapsed = " ".join(text.split())
    if collapsed:
        return collapsed[:200]
    return fallback


def _serialize_embedding(embedding: list[float] | None) -> str | None:
    if embedding is None:
        return None
    return "[" + ",".join(str(value) for value in embedding) + "]"


def _is_conflict_exc(exc: Exception) -> bool:
    if isinstance(exc, (asyncpg.UniqueViolationError, FileExistsError)):
        return True
    message = str(exc).lower()
    return "duplicate" in message or "unique" in message


class LongDocumentIngester:
    def __init__(
        self,
        chat_client: BaseChatClient,
        embedding_client: EmbeddingClient,
        content_generator: ContentGenerator,
        acl: ACLService,
        audit: AuditService,
        doc_store_root: str,
        *,
        max_document_size_mb: int = 50,
        max_token_per_node: int = 2000,
    ):
        self._chat_client = chat_client
        self._embedding_client = embedding_client
        self._content_generator = content_generator
        self._acl = acl
        self._audit = audit
        self._doc_store_root = Path(doc_store_root).expanduser().resolve()
        self._max_document_size_mb = max_document_size_mb
        self._max_token_per_node = max_token_per_node

    async def ingest(
        self,
        db: ScopedRepo,
        uri: str,
        source_path: str,
        ctx: RequestContext,
        tags: list[str] | None = None,
        *,
        allow_llm_tree: bool = True,
    ) -> DocumentIngestResponse:
        if isinstance(self._chat_client, NoOpChatClient):
            raise ServiceUnavailableError(
                "Long document ingestion requires a configured LLM API key"
            )

        source = self._preflight_source(source_path)
        self._validate_uri(uri)

        if not await self._acl.check_write_target(db, Scope.TEAM, "", ctx):
            raise ForbiddenError()

        duplicate = await db.fetchval("SELECT 1 FROM contexts WHERE uri = $1", uri)
        if duplicate is not None:
            raise ConflictError(f"Context {uri} already exists")

        self._doc_store_root.mkdir(parents=True, exist_ok=True)
        final_dir = self._doc_store_root / doc_dir_key(ctx.account_id, uri)
        created_final_dir = False
        try:
            try:
                final_dir.mkdir(parents=False, exist_ok=False)
            except FileExistsError as exc:
                raise ConflictError(f"Document directory already exists for {uri}") from exc
            created_final_dir = True

            copied_source = self._copy_source_file(source, final_dir)
            plain_text, markdown_text = self._extract_text(copied_source, final_dir)
            if not plain_text.strip():
                raise BadRequestError("Extracted document text is empty")

            generated = self._content_generator.generate(
                "resource",
                plain_text,
                metadata={"uri": uri, "source_path": str(source)},
            )
            embedding = await self._embed_l0(generated.l0)
            tree = await self.build_document_tree(
                markdown_text,
                plain_text,
                allow_llm=allow_llm_tree,
            )

            try:
                row = await db.fetchrow(
                    """
                    INSERT INTO contexts (
                        uri, context_type, scope, owner_space, account_id,
                        l0_content, l1_content, l2_content, file_path, tags, l0_embedding, status
                    ) VALUES (
                        $1, 'resource', 'team', '', current_setting('app.account_id'),
                        $2, $3, NULL, $4, $5, $6::vector, 'active'
                    )
                    RETURNING id
                    """,
                    uri,
                    generated.l0,
                    generated.l1,
                    str(final_dir),
                    tags,
                    _serialize_embedding(embedding),
                )
            except Exception as exc:
                if _is_conflict_exc(exc):
                    raise ConflictError(f"Context {uri} already exists") from exc
                raise

            if row is None:
                raise BadRequestError("Failed to create long document context")

            section_count = await self._persist_tree(db, row["id"], tree, ctx.account_id)
            await db.execute(
                """
                INSERT INTO change_events (context_id, account_id, change_type, actor)
                VALUES ($1, current_setting('app.account_id'), 'created', $2)
                """,
                row["id"],
                ctx.agent_id,
            )
            await self._audit.log_strict(
                db,
                ctx.agent_id,
                "create",
                uri,
                "success",
                metadata={
                    "context_type": "resource",
                    "scope": Scope.TEAM.value,
                    "section_count": section_count,
                    "file_path": str(final_dir),
                },
            )
            return DocumentIngestResponse(
                context_id=row["id"],
                uri=uri,
                section_count=section_count,
                file_path=str(final_dir),
            )
        except Exception:
            if created_final_dir:
                shutil.rmtree(final_dir, ignore_errors=True)
            raise

    def _preflight_source(self, source_path: str) -> Path:
        source = Path(source_path)
        try:
            stat = source.stat()
        except OSError as exc:
            raise BadRequestError(f"Invalid source_path: {source_path}") from exc
        if not source.is_file():
            raise BadRequestError(f"Invalid source_path: {source_path}")

        max_bytes = self._max_document_size_mb * 1024 * 1024
        if stat.st_size > max_bytes:
            raise BadRequestError("Document exceeds max_document_size_mb")
        return source

    @staticmethod
    def _validate_uri(uri: str) -> None:
        if not uri.startswith("ctx://resources/"):
            raise BadRequestError("resource URI must start with ctx://resources/")

    @staticmethod
    def _copy_source_file(source: Path, final_dir: Path) -> Path:
        suffix = source.suffix.lower()
        if suffix == ".pdf":
            target = final_dir / "source.pdf"
        elif suffix == ".txt":
            target = final_dir / "source.txt"
        elif suffix == ".md":
            target = final_dir / "source.md"
        else:
            raise BadRequestError(f"Unsupported document type: {suffix or '<none>'}")
        shutil.copy2(source, target)
        return target

    def _extract_text(self, source_file: Path, final_dir: Path) -> tuple[str, str]:
        suffix = source_file.suffix.lower()
        if suffix == ".pdf":
            plain_text, markdown_text = self._extract_pdf_text(source_file)
        elif suffix in {".txt", ".md"}:
            markdown_text = source_file.read_text(encoding="utf-8")
            plain_text = markdown_text
        else:
            raise BadRequestError(f"Unsupported document type: {suffix or '<none>'}")

        (final_dir / "extracted.txt").write_text(plain_text, encoding="utf-8")
        (final_dir / "extracted.md").write_text(markdown_text, encoding="utf-8")
        return plain_text, markdown_text

    @staticmethod
    def _extract_pdf_text(source_file: Path) -> tuple[str, str]:
        try:
            import fitz
        except ImportError as exc:
            raise ServiceUnavailableError("PyMuPDF is required for PDF ingestion") from exc

        chunks: list[str] = []
        with fitz.open(source_file) as document:
            for page in document:
                text = page.get_text("text")
                if text:
                    chunks.append(text)
        plain_text = "\n".join(chunks).strip()
        markdown_text = _pdf_to_markdownish_text(plain_text)
        return plain_text, markdown_text or plain_text

    async def _embed_l0(self, text: str) -> list[float] | None:
        try:
            return await self._embedding_client.embed(text)
        except Exception:
            logger.exception("L0 embedding failed; proceeding without embedding")
            return None

    async def build_document_tree(
        self,
        extracted_md: str,
        extracted_txt: str,
        *,
        allow_llm: bool = True,
    ) -> SectionNode:
        prompt = build_bounded_tree_prompt(extracted_md, extracted_txt)
        flat: list[dict[str, Any]] | None = None
        if allow_llm:
            try:
                response = await self._chat_client.complete(prompt, max_tokens=2000)
                payload = parse_llm_sections_json(response) if response else None
                flat = validate_flat_sections(payload, len(extracted_txt)) if payload else None
            except Exception:
                logger.exception("LLM tree generation failed; falling back deterministically")
                flat = None

        if not flat:
            flat = markdown_heading_fallback(extracted_md, extracted_txt, extracted_txt)
            flat = validate_flat_sections({"sections": flat}, len(extracted_txt)) if flat else None
        if not flat:
            flat = sequential_chunk_fallback(extracted_txt, self._max_token_per_node)
            flat = validate_flat_sections({"sections": flat}, len(extracted_txt)) if flat else None
        if not flat:
            raise BadRequestError("Unable to build document section tree")

        tree = flat_to_section_tree(flat, extracted_txt)
        if tree is None:
            raise BadRequestError("Unable to assemble document section tree")

        split_oversized_nodes(tree, extracted_txt, self._max_token_per_node)
        return tree

    async def _persist_tree(
        self,
        db: ScopedRepo,
        context_id,
        root: SectionNode,
        account_id: str,
    ) -> int:
        async def insert_node(node: SectionNode, parent_id: int | None) -> int:
            section_id = await db.fetchval(
                """
                INSERT INTO document_sections (
                    context_id, parent_id, node_id, title, depth,
                    start_offset, end_offset, summary, token_count, account_id
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10
                )
                RETURNING section_id
                """,
                context_id,
                parent_id,
                node.node_id,
                node.title,
                node.depth,
                node.start_offset,
                node.end_offset,
                node.summary,
                node.token_count,
                account_id,
            )
            count = 1
            for child in node.children:
                count += await insert_node(child, section_id)
            return count

        return await insert_node(root, None)
