"""Stage B/C: ingest one Cascade case into ContextHub, then apply root_change.

Faithful to MEME's own gold_facts — we do NOT synthesize clean "rule" nodes.
For each entity we store its gold_facts verbatim as memory nodes, split by role
using the dataset's own flags:

- root node            : the root entity's current-value fact (before).
- materialized node    : a derived entity's *current-value* fact (is_if_then=False).
                         Carries "... if the team lead changes, this would change"
                         but NOT the new value. This is the poisoned node: it is
                         the most direct answer to the question, gets a derived_from
                         edge, and must go stale on cascade.
- predeclaration node  : a derived entity's *is_if_then=True* fact ("if X changes,
                         Y will be <after>"). This carries the future value and is
                         the source ON uses to re-derive. Its premise becomes true
                         after the change, so it is NOT staled and gets NO edge.

Per the dataset (verified): every cascade target has exactly one is_if_then fact
whose value == the after gold answer, plus one or more is_if_then=False facts.

Edges follow dependency_edges_used: the materialized target depends_on its source.
root_change updates the root node + emits one `modified` change_event.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import uuid
from typing import Any

from contexthub.db.repository import ScopedRepo
from contexthub.llm.chat_client import BaseChatClient
from contexthub.services.change_detection_service import ChangeDetectionService
from contexthub.services.conversation_extraction_service import (
    ConversationExtractionService,
)
from contexthub.services.dependency_discovery_service import (
    CandidateFact,
    DependencyDiscoveryService,
)
from integrations.memebench.loader import CascadeCase

EVAL_AGENT = "meme-agent"


@dataclass
class IngestedGraph:
    account_id: str
    root_id: uuid.UUID | None
    # entity -> materialized current-value node id (stale-able)
    materialized: dict[str, uuid.UUID] = field(default_factory=dict)
    # entity -> predeclaration (is_if_then) node id (never staled)
    predeclarations: dict[str, uuid.UUID] = field(default_factory=dict)
    # id -> (entity, role) for every inserted fact node, for edge P/R reporting.
    node_meta: dict[uuid.UUID, tuple[str, str]] = field(default_factory=dict)
    # (dependency_id, dependent_id) edges actually persisted this ingest.
    persisted_edges: set[tuple[uuid.UUID, uuid.UUID]] = field(default_factory=set)
    # (node_id, text) for every fact node inserted, in insertion order. Populated
    # only by the raw-dialogue path (no gold entity labels) so edge P/R can be
    # scored by mapping node text back to gold entities by value (scoring-side
    # only; never fed to the system). Empty in the gold-facts path.
    inserted_nodes: list[tuple[uuid.UUID, str]] = field(default_factory=list)


def _gold_facts_for(case: CascadeCase, entity: str) -> list[dict[str, Any]]:
    return [
        g
        for sess in case.sessions
        for g in sess.get("gold_facts", [])
        if g.get("entity") == entity
    ]


async def _insert_memory(
    db: ScopedRepo, account_id: str, slug: str, text: str, embedding
) -> uuid.UUID:
    """Insert a memory node with L0/L1/L2 (full verbatim in L2) + precomputed embedding.

    Embed the full fact text (not the 80-char L0) so retrieval sees the whole
    sentence, including the conditional clause. Embedding is precomputed (batched
    by the caller) to minimize round-trips to a flaky embedding endpoint.
    """
    mem_id = uuid.uuid4()
    uri = f"ctx://agent/{EVAL_AGENT}/memories/{slug}-{uuid.uuid4().hex[:6]}"
    await db.execute(
        """
        INSERT INTO contexts (id, uri, context_type, scope, owner_space, account_id,
                              l0_content, l1_content, l2_content)
        VALUES ($1, $2, 'memory', 'agent', $3, $4, $5, $6, $7)
        """,
        mem_id, uri, EVAL_AGENT, account_id, text[:80], text[:300], text,
    )
    if embedding is not None:
        emb_str = "[" + ",".join(str(x) for x in embedding) + "]"
        await db.execute(
            "UPDATE contexts SET l0_embedding = $1::vector WHERE id = $2", emb_str, mem_id
        )
    return mem_id


async def _embed_all(embed_batch, texts: list[str]) -> list:
    """Batch-embed a list of texts; returns aligned embeddings (or Nones)."""
    if not texts:
        return []
    return await embed_batch(texts)


def _current_value_fact(facts: list[dict[str, Any]], after: Any) -> dict[str, Any] | None:
    """The materialized current-value fact: not is_if_then, prefer value != after."""
    candidates = [g for g in facts if not g.get("is_if_then")]
    if not candidates:
        return None
    non_after = [g for g in candidates if g.get("value") != after]
    return (non_after or candidates)[0]


def _predeclaration_fact(facts: list[dict[str, Any]]) -> dict[str, Any] | None:
    for g in facts:
        if g.get("is_if_then"):
            return g
    return None


async def ingest_case(
    db: ScopedRepo,
    case: CascadeCase,
    account_id: str,
    embed_batch,
    *,
    edge_mode: str = "gold",
    discovery: DependencyDiscoveryService | None = None,
) -> IngestedGraph:
    """Two-phase: collect all node (slug, entity, role, text) → one batch embed → insert.

    edge_mode:
      - "gold"       : build derived_from edges from MEME's dependency_edges_used
                       (oracle shortcut; the "dependency known" upper bound).
      - "discovered" : ignore the gold graph; for each non-root fact node, ask the
                       core DependencyDiscoveryService which already-inserted fact
                       node(s) it is derived from, and build those edges. This is
                       what MEME's baselines face — no dependency graph supplied.
      - "discovered_tiered" : same as discovered but the caller passes a
                       conditional-aware discovery service (tier-0 syntactic
                       routing; LLM still decides). Ingest path identical.
      - "discovered_hard"   : same, but conditional-hard service (syntactic
                       hard-exclude of conditional-looking facts). Ingest path
                       identical; only the injected service differs.
    """
    if edge_mode not in ("gold", "discovered", "discovered_tiered", "discovered_hard"):
        raise ValueError(
            "edge_mode must be 'gold'/'discovered'/'discovered_tiered'/'discovered_hard', "
            f"got {edge_mode!r}"
        )
    if edge_mode.startswith("discovered") and discovery is None:
        raise ValueError(f"edge_mode={edge_mode!r} requires a discovery service")

    graph = IngestedGraph(account_id=account_id, root_id=None)
    node_by_entity: dict[str, uuid.UUID] = {}

    # Phase 1: collect node specs (slug, text, role, entity).
    specs: list[tuple[str, str, str, str | None]] = []  # (slug, text, role, entity)

    root_facts = _gold_facts_for(case, case.root)
    root_before = (case.entities.get(case.root).before
                   if case.entities.get(case.root) else case.root_change.get("before"))
    root_fact = _current_value_fact(root_facts, case.root_change.get("after"))
    root_text = (root_fact or {}).get("fact_text") or f"The {case.root} is {root_before}."
    specs.append((f"root-{case.root}", root_text, "root", case.root))

    seen_targets: set[str] = set()
    for edge in sorted(case.edges, key=lambda e: e.hop):
        target = edge.target
        if target in seen_targets:
            continue
        seen_targets.add(target)
        facts = _gold_facts_for(case, target)
        if not facts:
            continue
        ent = case.entities.get(target)
        after_val = ent.after if ent else None
        cur = _current_value_fact(facts, after_val)
        if cur:
            specs.append((f"cur-{target}", cur.get("fact_text") or cur.get("original_seed") or "", "cur", target))
        pre = _predeclaration_fact(facts)
        if pre:
            specs.append((f"pre-{target}", pre.get("fact_text") or pre.get("original_seed") or "", "pre", target))

    # Phase 2: one batched embedding call for all node texts.
    embeddings = await _embed_all(embed_batch, [s[1] for s in specs])

    # Phase 3: insert nodes with their embeddings; keep insertion order + texts.
    inserted: list[tuple[uuid.UUID, str]] = []  # (node_id, text) in insertion order
    for (slug, text, role, entity), emb in zip(specs, embeddings):
        node_id = await _insert_memory(db, account_id, slug, text, emb)
        graph.node_meta[node_id] = (entity or "", role)
        inserted.append((node_id, text))
        if role == "root":
            graph.root_id = node_id
            node_by_entity[case.root] = node_id
        elif role == "cur":
            graph.materialized[entity] = node_id
            node_by_entity[entity] = node_id
        elif role == "pre":
            graph.predeclarations[entity] = node_id

    # Phase 4: build derived_from edges.
    if edge_mode == "gold":
        edges = _gold_edges(case, node_by_entity, graph)
    else:  # discovered / discovered_tiered — differ only by injected service
        edges = await _discovered_edges(discovery, inserted)

    for tgt_id, src_id in edges:
        await db.execute(
            """
            INSERT INTO dependencies (dependent_id, dependency_id, dep_type)
            VALUES ($1, $2, 'derived_from')
            ON CONFLICT (dependent_id, dependency_id, dep_type) DO NOTHING
            """,
            tgt_id, src_id,
        )
        graph.persisted_edges.add((src_id, tgt_id))

    return graph


def _gold_edges(
    case: CascadeCase, node_by_entity: dict[str, uuid.UUID], graph: IngestedGraph
) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """MEME gold edges: materialized target depends_on its source node."""
    out: list[tuple[uuid.UUID, uuid.UUID]] = []
    for edge in case.edges:
        src_id = node_by_entity.get(edge.source)
        tgt_id = graph.materialized.get(edge.target)
        if src_id is None or tgt_id is None:
            continue
        out.append((tgt_id, src_id))  # (dependent, dependency)
    return out


async def _discovered_edges(
    discovery: DependencyDiscoveryService, inserted: list[tuple[uuid.UUID, str]]
) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """Discover edges write-time: each node is judged against earlier nodes only.

    Candidates for node i are nodes 0..i-1 (insertion order = arrival order), so
    the graph is built incrementally exactly as a live write path would. Returns
    (dependent_id, dependency_id) pairs. Filler nodes are not part of `inserted`.
    """
    out: list[tuple[uuid.UUID, uuid.UUID]] = []
    for i in range(1, len(inserted)):
        new_id, new_text = inserted[i]
        candidates = [CandidateFact(id=nid, text=t) for nid, t in inserted[:i]]
        source_ids = await discovery.discover_sources(new_text, candidates)
        for src_id in source_ids:
            out.append((new_id, src_id))  # (dependent, dependency)
    return out


def edge_pr(case: CascadeCase, graph: IngestedGraph) -> dict[str, float | int]:
    """Precision/recall of persisted edges vs MEME gold, at node granularity.

    Gold edges connect the source entity's node to the *materialized* target node
    (predeclaration nodes have no gold in-edge). An edge the discovery persisted
    onto a predeclaration node — plausible since its text mentions the upstream —
    is a false positive here, which is faithful: that mislink also mis-stales the
    re-derivation source downstream, so Step-1 P/R and Step-2 delta stay aligned.
    """
    node_by_entity: dict[str, uuid.UUID] = {}
    if graph.root_id is not None:
        node_by_entity[case.root] = graph.root_id
    node_by_entity.update(graph.materialized)
    gold: set[tuple[uuid.UUID, uuid.UUID]] = set()
    for edge in case.edges:
        src = node_by_entity.get(edge.source)
        tgt = graph.materialized.get(edge.target)
        if src is not None and tgt is not None:
            gold.add((src, tgt))
    pred = graph.persisted_edges
    tp = len(pred & gold)
    precision = tp / len(pred) if pred else (1.0 if not gold else 0.0)
    recall = tp / len(gold) if gold else 1.0
    return {
        "n_gold": len(gold),
        "n_pred": len(pred),
        "n_tp": tp,
        "precision": precision,
        "recall": recall,
    }


def edge_pr_raw(case: CascadeCase, graph: IngestedGraph) -> dict[str, float | int]:
    """Approximate edge P/R for the raw-dialogue path (no entity labels).

    Nodes carry no gold entity tag here, so map each node to gold entities by
    VALUE: a node "is about" entity E if E's before-value string appears in the
    node text (same rule the extraction probe validated). Then:

    - gold edges are the (source, target) entity pairs in case.edges;
    - a gold pair is RECALLED if some persisted node-edge (src, tgt) has src
      mapping to the source entity and tgt to the target entity;
    - PRECISION counts persisted edges whose two endpoints map to a gold pair,
      over all persisted edges.

    Value mapping is many-to-one (repeats, explanatory lines), so this is an
    approximate diagnostic, not an exact count. It is scoring-side only and is
    never shown to the system.
    """
    def entities_of(text: str) -> set[str]:
        tn = _norm(text)
        out: set[str] = set()
        for name, ent in case.entities.items():
            bv = _norm(ent.before)
            if bv and bv in tn:
                out.add(name)
        return out

    node_ents: dict[uuid.UUID, set[str]] = {
        nid: entities_of(t) for nid, t in graph.inserted_nodes
    }
    gold_pairs = {(e.source, e.target) for e in case.edges}

    recalled = 0
    for src_e, tgt_e in gold_pairs:
        hit = any(
            src_e in node_ents.get(s, set()) and tgt_e in node_ents.get(t, set())
            for s, t in graph.persisted_edges
        )
        recalled += int(hit)

    tp_edges = 0
    for s, t in graph.persisted_edges:
        pair_hit = any(
            se in node_ents.get(s, set()) and te in node_ents.get(t, set())
            for se, te in gold_pairs
        )
        tp_edges += int(pair_hit)

    n_gold = len(gold_pairs)
    n_pred = len(graph.persisted_edges)
    precision = tp_edges / n_pred if n_pred else (1.0 if not n_gold else 0.0)
    recall = recalled / n_gold if n_gold else 1.0
    return {
        "n_gold": n_gold,
        "n_pred": n_pred,
        "n_tp": recalled,
        "precision": precision,
        "recall": recall,
    }


async def ingest_filler(
    db: ScopedRepo, case: CascadeCase, account_id: str, embed_batch, *, granularity: str = "session"
) -> int:
    """Ingest filler sessions as noise memory nodes that compete in retrieval.

    Reproduces the baseline's real predicament: the useful predeclaration is
    buried among unrelated conversations. granularity="session" stores one node
    per filler session (concatenated user turns); "turn" stores one per user turn.
    Batches all filler embeddings into one call. Returns the number of noise nodes.
    """
    chunks: list[str] = []
    for sess in case.sessions:
        if sess.get("type") != "filler":
            continue
        turns = [t.get("content", "") for t in sess.get("conversation", []) if t.get("role") == "user"]
        turns = [t for t in turns if t and t.strip()]
        if not turns:
            continue
        if granularity == "turn":
            chunks.extend(turns)
        else:
            chunks.append("\n".join(turns))

    if not chunks:
        return 0
    embeddings = await _embed_all(embed_batch, chunks)
    for chunk, emb in zip(chunks, embeddings):
        await _insert_memory(db, account_id, "filler", chunk, emb)
    return len(chunks)


async def apply_root_change(
    db: ScopedRepo, case: CascadeCase, graph: IngestedGraph, embed
) -> None:
    """Update root node to its after value + emit a `modified` change_event.

    Uses the root_change event's own fact_text (from session 4) so the upstream
    change text is verbatim from MEME. Single embed call (one text).
    """
    after = case.root_change.get("after")
    before = case.root_change.get("before")
    root_facts = _gold_facts_for(case, case.root)
    after_fact = next((g for g in root_facts if g.get("value") == after), None)
    after_text = (after_fact or {}).get("fact_text") or f"The {case.root} is {after}."

    embedding = await embed(after_text)
    emb_str = "[" + ",".join(str(x) for x in embedding) + "]" if embedding is not None else None
    await db.execute(
        """
        UPDATE contexts
        SET l0_content = $1, l1_content = $2, l2_content = $3,
            l0_embedding = COALESCE($4::vector, l0_embedding), updated_at = NOW()
        WHERE id = $5
        """,
        after_text[:80], after_text[:300], after_text, emb_str, graph.root_id,
    )
    await db.execute(
        """
        INSERT INTO change_events (context_id, account_id, change_type, actor, diff_summary, metadata)
        VALUES ($1, $2, 'modified', 'meme_eval', $3, $4)
        """,
        graph.root_id, graph.account_id, f"{case.root}: {before} -> {after}",
        {"before": before, "after": after, "entity": case.root},
    )


# ---------------------------------------------------------------------------
# Raw-dialogue (mode B) path: no gold_facts oracle, no entity schema.
#
# Evidence sessions are extracted into facts by the extractor (no entity list),
# ingested per-session in timeline order so before-values survive later changes,
# and linked by the same write-time DependencyDiscoveryService as the gold path.
# The root change arrives as one more extracted session; a zero-oracle detector
# decides which already-stored fact it supersedes and fires the event on THAT
# node. Nothing on this path reads case.entities / gold_facts / dependency_edges.
# ---------------------------------------------------------------------------

_CHANGE_SESSION_ID = "evidence_change+delete_event"


def _norm(s) -> str:
    return " ".join(str(s or "").casefold().split())


def _session_text(sess: dict[str, Any]) -> str:
    parts: list[str] = []
    for turn in sess.get("conversation", []):
        role = turn.get("role", "")
        content = turn.get("content", "")
        if content:
            parts.append(f"{role}: {content}")
    return "\n".join(parts)


def _evidence_sessions(case: CascadeCase) -> list[dict[str, Any]]:
    return [s for s in case.sessions if s.get("type") != "filler"]


def _split_evidence(case: CascadeCase) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Return (pre-change evidence sessions, the change session).

    The change is always the last evidence session (id ``evidence_change+
    delete_event``); prefer matching the id, else fall back to the final
    evidence session in timeline order.
    """
    ev = _evidence_sessions(case)
    change = next((s for s in ev if s.get("session_id") == _CHANGE_SESSION_ID), None)
    if change is None and ev:
        change = ev[-1]
    pre = [s for s in ev if s is not change]
    return pre, change


async def ingest_case_raw(
    db: ScopedRepo,
    case: CascadeCase,
    account_id: str,
    embed_batch,
    extractor: ConversationExtractionService,
    discovery: DependencyDiscoveryService,
) -> IngestedGraph:
    """Ingest a case from raw dialogue (mode B), pre-change sessions only.

    For each pre-change evidence session, extract facts (no entity schema) and
    insert them, then discover derived_from edges against all earlier-inserted
    nodes. Timeline-incremental: a before-value stated early is stored before a
    later session restates a newer value, so both survive as separate nodes.
    The change session is handled by apply_root_change_raw.
    """
    graph = IngestedGraph(account_id=account_id, root_id=None)
    pre_sessions, _ = _split_evidence(case)

    for sess in pre_sessions:
        facts = await extractor.extract(_session_text(sess))
        texts = [f.text for f in facts if f.text and f.text.strip()]
        if not texts:
            continue
        embeddings = await _embed_all(embed_batch, texts)
        for text, emb in zip(texts, embeddings):
            new_id = await _insert_memory(db, account_id, "fact", text, emb)
            # discover edges against everything inserted before this node
            candidates = [CandidateFact(id=nid, text=t) for nid, t in graph.inserted_nodes]
            source_ids = await discovery.discover_sources(text, candidates)
            for src_id in source_ids:
                await db.execute(
                    """
                    INSERT INTO dependencies (dependent_id, dependency_id, dep_type)
                    VALUES ($1, $2, 'derived_from')
                    ON CONFLICT (dependent_id, dependency_id, dep_type) DO NOTHING
                    """,
                    new_id, src_id,
                )
                graph.persisted_edges.add((src_id, new_id))
            graph.inserted_nodes.append((new_id, text))

    return graph


async def apply_root_change_raw(
    db: ScopedRepo,
    case: CascadeCase,
    account_id: str,
    graph: IngestedGraph,
    embed_batch,
    extractor: ConversationExtractionService,
    change_chat: BaseChatClient,
) -> list[uuid.UUID]:
    """Ingest the change session, then fire `modified` on each superseded node.

    Mode-B change arrival, matching a live write path with no oracle:
    1. extract the change session into facts and insert them as new nodes
       (they coexist with the old values, as in a flat memory system);
    2. for each new fact, ask detect_superseded over ALL already-stored nodes
       (full candidate set) which stored fact it makes outdated;
    3. emit one `modified` change_event on each superseded node. Propagation
       then marks its derived_from dependents stale (mechanism unchanged).

    Returns the list of superseded node ids (for reporting; empty means the
    detector found nothing to invalidate, i.e. no cascade will fire).
    """
    _, change_sess = _split_evidence(case)
    if change_sess is None:
        return []

    facts = await extractor.extract(_session_text(change_sess))
    texts = [f.text for f in facts if f.text and f.text.strip()]
    if not texts:
        return []

    # Candidate set = every node stored so far (full, zero-oracle).
    prior = list(graph.inserted_nodes)
    detection = ChangeDetectionService(change_chat)

    embeddings = await _embed_all(embed_batch, texts)
    superseded: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for text, emb in zip(texts, embeddings):
        new_id = await _insert_memory(db, account_id, "change", text, emb)
        candidates = [CandidateFact(id=nid, text=t) for nid, t in prior]
        hit_ids = await detection.detect_superseded(text, candidates)
        for hid in hit_ids:
            if hid not in seen:
                seen.add(hid)
                superseded.append(hid)
        graph.inserted_nodes.append((new_id, text))

    for old_id in superseded:
        await db.execute(
            """
            INSERT INTO change_events (context_id, account_id, change_type, actor, diff_summary, metadata)
            VALUES ($1, $2, 'modified', 'meme_eval', $3, $4)
            """,
            old_id, account_id, f"root change: {case.root_change.get('before')} -> {case.root_change.get('after')}",
            {"before": case.root_change.get("before"), "after": case.root_change.get("after")},
        )
    if superseded:
        graph.root_id = superseded[0]  # for reporting only
    return superseded
