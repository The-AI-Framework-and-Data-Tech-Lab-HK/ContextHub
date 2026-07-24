"""Load the MEME benchmark and extract Cascade cases.

MEME (arXiv:2605.12477) publishes episodes as a JSON list. Each episode is a
multi-session conversation with an upstream ``root_change`` that should cascade
to downstream derived entities via ``dependency_edges_used``.

This loader keeps only what the Cascade external-control eval needs: for each
``Cas`` task it packages the dependency chain (edges + per-entity before/after
values), the upstream change, and the paired before/after questions used for
MEME's trivial-pass judging.

Data source: https://huggingface.co/datasets/meme-benchmark/MEME
The ``nofiller`` variant (meme_nofiller.json, ~4.6MB, non-LFS) is a plain file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

DEFAULT_DATA_PATH = Path(
    "/Users/sherrylin/Documents/PythonProjects/public/MEME/meme_nofiller.json"
)


@dataclass
class Edge:
    """A derived_from dependency edge from ``source`` entity to ``target``."""

    source: str
    target: str
    hop: int
    pattern: str
    is_2hop_middle: bool = False


@dataclass
class Entity:
    """An entity node with its before/after values and cascade metadata."""

    name: str
    before: Any
    after: Any
    cascade_source: str | None = None
    hop: int | None = None


@dataclass
class Question:
    """A before- or after-change question (from the paired question sets)."""

    task_type: str
    entity: list[str]
    question: str
    expected_answer: str
    hop: int | None = None


@dataclass
class CascadeCase:
    """One Cascade (``Cas``) task, self-contained for the eval pipeline.

    A single episode can yield multiple Cascade cases (one per ``Cas`` task);
    they share the same episode graph but score a different target entity.
    """

    episode_id: str
    domain: str
    hop: int
    # upstream change that triggers the cascade
    root: str
    root_change: dict[str, Any]
    cascade_source: str
    # the scored target
    target_entity: str
    gold_answer: str
    question: str
    # trivial-pass: the before-change question for the same target
    before_question: Question | None
    after_question: Question | None
    # graph material for ingestion (whole-episode; shared across the episode's cases)
    edges: list[Edge]
    entities: dict[str, Entity]
    sessions: list[dict[str, Any]] = field(default_factory=list)


def load_episodes(path: str | Path = DEFAULT_DATA_PATH) -> list[dict[str, Any]]:
    """Load raw MEME episodes (a JSON list of episode objects)."""
    with Path(path).open(encoding="utf-8") as handle:
        episodes = json.load(handle)
    if not isinstance(episodes, list):
        raise ValueError(f"Expected a JSON list of episodes, got {type(episodes).__name__}")
    return episodes


def _parse_entities(raw: dict[str, Any]) -> dict[str, Entity]:
    out: dict[str, Entity] = {}
    for name, meta in (raw or {}).items():
        if not isinstance(meta, dict):
            continue
        out[name] = Entity(
            name=name,
            before=meta.get("before"),
            after=meta.get("after"),
            cascade_source=meta.get("cascade_source"),
            hop=meta.get("hop"),
        )
    return out


def _parse_edges(raw: list[dict[str, Any]]) -> list[Edge]:
    return [
        Edge(
            source=e["source"],
            target=e["target"],
            hop=e.get("hop", 1),
            pattern=e.get("pattern", ""),
            is_2hop_middle=bool(e.get("is_2hop_middle", False)),
        )
        for e in (raw or [])
    ]


def _find_question(qset: dict[str, Any], target_entity: str) -> Question | None:
    """Find the Cas question for ``target_entity`` in a before/after question set.

    ``expected_answer`` (before) and ``gold_answer`` (after) name the same field
    differently in the dataset; normalise both to ``expected_answer``.
    """
    for q in (qset or {}).get("questions", []):
        if q.get("task_type") != "Cas":
            continue
        if target_entity in (q.get("entity") or []):
            return Question(
                task_type="Cas",
                entity=list(q.get("entity") or []),
                question=q.get("question", ""),
                expected_answer=q.get("expected_answer") or q.get("gold_answer") or "",
                hop=q.get("hop"),
            )
    return None


def extract_cascade_cases(
    episodes: list[dict[str, Any]],
    hop: int | None = None,
) -> list[CascadeCase]:
    """Extract one CascadeCase per ``Cas`` task, optionally filtered by hop."""
    cases: list[CascadeCase] = []
    for ep in episodes:
        entities = _parse_entities(ep.get("entities", {}))
        edges = _parse_edges(ep.get("dependency_edges_used", []))
        before_q = ep.get("before_questions", {})
        after_q = ep.get("after_questions", {})
        for task in ep.get("tasks", []):
            if task.get("type") != "Cas":
                continue
            targets = task.get("target_entities") or []
            if not targets:
                continue
            target = targets[0]
            task_hop = task.get("hop", 1)
            if hop is not None and task_hop != hop:
                continue
            cases.append(
                CascadeCase(
                    episode_id=ep["episode_id"],
                    domain=ep["domain"],
                    hop=task_hop,
                    root=ep.get("root", ""),
                    root_change=ep.get("root_change", {}),
                    cascade_source=task.get("cascade_source", ""),
                    target_entity=target,
                    gold_answer=task.get("gold_answer", ""),
                    question=task.get("question_template", ""),
                    before_question=_find_question(before_q, target),
                    after_question=_find_question(after_q, target),
                    edges=edges,
                    entities=entities,
                    sessions=ep.get("sessions", []),
                )
            )
    return cases
