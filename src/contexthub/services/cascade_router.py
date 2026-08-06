"""Build-side confidence cascade router.

Each dependency-graph build step runs its cheapest tier first, reads a RUNTIME
confidence signal, and escalates to a stronger tier only when confidence < tau.
This is the build-side analogue of the propagation-side cascade (a free regex
filter runs first; only uncertain items reach the strong LLM). Sweeping tau
traces a cost-quality frontier; tau is fixed at deploy time, never tuned on eval.

Why a cascade rather than a static per-method optimizer: choosing one method per
step would need each item's utility u, which is not observable at write time
without gold labels (calibrating u on held-out data is leakage). The cascade
needs only cost (runtime-estimable, cheaper tier first) and the cheap tier's
confidence (runtime-observable) — never gold. Formally it is the deployable dual
of the MCKP Lagrangian per-item rule: "escalate iff Delta_u/Delta_c > lambda"
becomes "escalate iff (1 - confidence) > tau".

Confidence signals are structural and gold-free: content-word overlap and cosine
similarity (computed here, no LLM call) plus the cheap tier's own decisiveness.
No logprob is used (proxies may not transmit it, some models never return it, and
its scale is not comparable across models).

Four variables:
  1 extraction           : regex/template -> weak LLM -> strong LLM
  2 coref/disambiguation : literal/alias -> spaCy NER -> LLM
  3 edge discovery       : regex hard-block -> cheap LLM -> strong LLM
  4 candidate selection  : lexical blocking -> embed_topk -> full pool

The router depends only on core services (DependencyDiscoveryService,
ConversationExtractionService) and takes candidates as CandidateFact lists (whose
optional .embedding the candidate-selection step ranks by), so it serves both the
production write path and evaluation harnesses. spaCy is an OPTIONAL dependency:
if it is not installed, variable-2's middle tier is skipped (literal -> LLM) and
the router still works.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
import uuid

from contexthub.services.conversation_extraction_service import (
    ConversationExtractionService,
    ExtractedFact,
)
from contexthub.services.dependency_discovery_service import (
    CandidateFact,
    DependencyDiscoveryService,
)

# --------------------------------------------------------------------------- #
# Structural, gold-free confidence signals (no LLM, no network).
# --------------------------------------------------------------------------- #

_STOP = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being", "to",
    "of", "in", "on", "at", "for", "and", "or", "but", "if", "then", "this",
    "that", "these", "those", "it", "its", "as", "with", "by", "from", "will",
    "would", "my", "i", "you", "he", "she", "they", "we", "user", "s",
}


def _content_words(text: str) -> set[str]:
    return {
        w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
        if w not in _STOP and len(w) > 2
    }


def _cosine(a: list[float] | None, b: list[float] | None) -> float:
    if a is None or b is None:
        return -1.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return -1.0
    return dot / (na * nb)


def _overlap_coeff(a: str, b: str) -> float:
    """Overlap coefficient of content words: |A∩B| / min(|A|,|B|).

    A structural, gold-free support signal for a proposed edge, robust to length
    differences. 0 when either side has no content word. A proposed edge between
    two facts sharing few content words is the riskiest false positive and should
    be escalated (the D.5b insight: purely-semantic false edges are LOW support).
    """
    wa, wb = _content_words(a), _content_words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / min(len(wa), len(wb))


# --------------------------------------------------------------------------- #
# Route results.
# --------------------------------------------------------------------------- #


@dataclass
class CandidateRoute:
    """Result of variable-4 routing: which candidates to hand the edge judge."""

    candidates: list[CandidateFact]
    tier: str  # "block" | "embed_topk" | "full"
    confidence: float


@dataclass
class EdgeRoute:
    """Result of variable-3 routing: the discovered derived_from source ids."""

    sources: list[uuid.UUID]
    tier: str  # "regex" | "cheap_none" | "cheap" | "strong"
    confidence: float


@dataclass
class ExtractRoute:
    """Result of variable-1 routing: the extracted facts for one session."""

    facts: list[ExtractedFact]
    tier: str  # "regex" | "cheap" | "strong"
    confidence: float


@dataclass
class DisambRoute:
    """Result of variable-2 routing: mention text -> resolved entity name."""

    resolution: str | None
    tier: str  # "literal" | "spacy" | "llm" | "none"
    confidence: float


# --------------------------------------------------------------------------- #
# Variable 4 — candidate selection.
# --------------------------------------------------------------------------- #


def route_candidate_selection(
    new_text: str,
    new_emb: list[float] | None,
    pool: list[CandidateFact],
    tau: float,
    *,
    k: int = 10,
    min_cands: int = 3,
) -> CandidateRoute:
    """Pick the candidate shortlist for the new node's edge judgement.

    Ladder (cheapest first), escalate when confidence < tau:
      "block"      : lexical blocking (content-word overlap). Cheapest — a tiny
                     shortlist, fewest prompt tokens for the LLM judge.
      "embed_topk" : top-k candidates by cosine to new_emb. Wider, similarity-
                     ranked. Escalated to when blocking is thin (< min_cands) or
                     its best candidate is weakly similar.
      "full"       : the whole pool. Escalated to only when NOTHING is confidently
                     similar — widest net so a low-similarity true upstream is not
                     missed (recall).

    Confidence = max cosine among the current tier's candidates (structural, no
    LLM, no gold). Candidates carry their embedding (CandidateFact.embedding);
    a candidate with no embedding scores cosine -1 and never raises confidence.

    Higher tau -> escalate more often -> more prompt tokens + higher recall.
    tau = 0 stays at blocking; tau = 1 forces full (the O(N^2) baseline).
    """
    if not pool:
        return CandidateRoute([], "block", 1.0)

    nw = _content_words(new_text)
    block = [c for c in pool if _content_words(c.text) & nw]
    conf_block = max((_cosine(new_emb, c.embedding) for c in block), default=0.0)
    conf_block = max(conf_block, 0.0)
    if len(block) >= min_cands and conf_block >= tau:
        return CandidateRoute(block, "block", conf_block)

    # Escalate: rank the full pool by cosine (embeddings already computed at insert
    # time, so selection itself costs no extra LLM/embedding call).
    scored = sorted(
        ((_cosine(new_emb, c.embedding), c) for c in pool),
        key=lambda t: t[0],
        reverse=True,
    )
    conf_topk = max(scored[0][0], 0.0)
    if conf_topk >= tau:
        return CandidateRoute([c for _, c in scored[:k]], "embed_topk", conf_topk)

    # Nothing is confidently similar: widest net for recall.
    return CandidateRoute(list(pool), "full", conf_topk)


# --------------------------------------------------------------------------- #
# Variable 3 — edge discovery.
# --------------------------------------------------------------------------- #


async def route_edge_discovery(
    new_text: str,
    candidates: list[CandidateFact],
    tau: float,
    *,
    cheap: DependencyDiscoveryService,
    strong: DependencyDiscoveryService,
) -> EdgeRoute:
    """Decide which candidates the new fact is derived from.

    Ladder (cheapest first), escalate when confidence < tau:
      "regex"      : free syntactic hard-block. A conditional-rule fact ("if X
                     changes, Y will be ...") gets NO in-edge — no LLM call. The
                     router owns tier-0 (via _looks_conditional) and applies it
                     exactly once, so the injected cheap/strong services must be
                     PLAIN (no conditional flags).
      "cheap"/"cheap_none" : cheap LLM judges. A decisive NONE is trusted (most
                     facts derive from nothing) — no escalation.
      "strong"     : if the cheap tier proposes an edge whose structural support
                     is weak (< tau), that edge is the riskiest false positive, so
                     the strong LLM re-judges the node.

    Confidence of a proposed edge set = min overlap coefficient over the proposed
    edges (the weakest-supported edge sets the confidence). Structural, no logprob.

    tau = 0 trusts the cheap tier (cheap-only baseline); tau = 1 escalates every
    proposed edge (strong-only baseline).
    """
    # tier 0: free regex hard-block on the NEW fact.
    if DependencyDiscoveryService._looks_conditional(new_text):
        return EdgeRoute([], "regex", 1.0)
    if not candidates:
        return EdgeRoute([], "cheap_none", 1.0)

    # tier 1: cheap LLM.
    cheap_sources = await cheap.discover_sources(new_text, candidates)
    if not cheap_sources:
        # Decisive NONE: trust it (recall-safe — the cheap tier declined to link).
        return EdgeRoute([], "cheap_none", 1.0)

    by_id = {c.id: c.text for c in candidates}
    conf = min(_overlap_coeff(new_text, by_id[s]) for s in cheap_sources if s in by_id)
    if conf >= tau:
        return EdgeRoute(cheap_sources, "cheap", conf)

    # tier 2: strong LLM re-judges the same candidate set.
    strong_sources = await strong.discover_sources(new_text, candidates)
    return EdgeRoute(strong_sources, "strong", conf)


# --------------------------------------------------------------------------- #
# Variable 1 — extraction.
# --------------------------------------------------------------------------- #

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")
# "X is/are/was/were Y" — the simplest declarative the regex tier lifts as a
# self-contained fact with high confidence.
_X_IS_Y = re.compile(r"^[A-Z][\w ]{1,60}?\s+(is|are|was|were)\s+.{1,120}$")
# Structural low-confidence markers: a sentence with any of these needs the LLM
# (a pronoun to resolve, a conjunction/conditional binding two clauses).
_PRONOUN = re.compile(r"\b(he|she|it|they|him|her|them|his|its|their|this|that)\b", re.I)
_CONJ = re.compile(r"\b(and|but|because|so|which|who|whose|if|when|while)\b", re.I)
_GREETING = re.compile(r"^(hi|hello|hey|thanks|thank you|sure|ok|okay|yeah|great)\b", re.I)


def _regex_extract_sentence(sent: str) -> ExtractedFact | None:
    """Lift one sentence into a fact with the regex tier, or None if unconfident.

    Confident only for a clean "X is Y" declarative with no pronoun / conjunction
    / conditional / greeting — deliberately high-precision / low-recall. The cheap
    tier handles the easy sentences; the cascade escalates the rest.
    """
    s = sent.strip()
    if not s or _GREETING.search(s):
        return None
    if _PRONOUN.search(s) or _CONJ.search(s):
        return None
    if _X_IS_Y.match(s):
        return ExtractedFact(text=s)
    return None


async def route_extraction(
    session_text: str,
    tau: float,
    *,
    cheap: ConversationExtractionService,
    strong: ConversationExtractionService,
) -> ExtractRoute:
    """Extract facts from one raw session, cheapest tier first.

    Ladder: regex/template -> weak LLM -> strong LLM. Confidence = fraction of
    sentences the regex tier confidently lifted (structural, gold-free). A session
    of clean declaratives stays regex-only; one full of pronouns / multi-clause /
    conditional sentences escalates. tau = 0 keeps regex; tau = 1 forces strong.
    """
    sents = [s for s in _SENT_SPLIT.split(session_text or "") if s.strip()]
    if not sents:
        return ExtractRoute([], "regex", 1.0)
    lifted = [f for f in (_regex_extract_sentence(s) for s in sents) if f]
    conf = len(lifted) / len(sents)
    if conf >= tau:
        return ExtractRoute(lifted, "regex", conf)

    cheap_facts = await cheap.extract(session_text)
    # Cheap-tier decisiveness: if it produced facts, trust it unless tau demands
    # the strong tier for every session (tau close to 1).
    if cheap_facts and conf >= tau - 0.5:
        return ExtractRoute(cheap_facts, "cheap", conf)
    strong_facts = await strong.extract(session_text)
    return ExtractRoute(strong_facts, "strong", conf)


# --------------------------------------------------------------------------- #
# Variable 2 — coref / disambiguation. spaCy is an OPTIONAL dependency.
# --------------------------------------------------------------------------- #

_SPACY = None
_SPACY_TRIED = False


def _spacy_nlp():
    """Lazy spaCy singleton, or None if spaCy / the model is not installed.

    Loaded once on first use. When unavailable the router skips variable-2's
    middle tier (literal -> LLM), so spaCy stays an optional dependency.
    """
    global _SPACY, _SPACY_TRIED
    if _SPACY_TRIED:
        return _SPACY
    _SPACY_TRIED = True
    try:
        import spacy

        _SPACY = spacy.load("en_core_web_sm", disable=["lemmatizer", "textcat"])
    except Exception:
        _SPACY = None
    return _SPACY


_PRONOUN_ONLY = re.compile(r"^\s*(he|she|it|they|him|her|them|his|its|their)\s*$", re.I)


async def route_disambiguation(
    mention: str,
    context: str,
    entity_pool: list[str],
    tau: float,
    *,
    llm: DependencyDiscoveryService,
) -> DisambRoute:
    """Resolve a mention to an entity in the pool, cheapest tier first.

    Ladder (cheapest first), escalate when confidence < tau:
      "literal" : exact / substring match of the mention against a pool entity.
                  Zero cost; confidence 1.0 when it fires.
      "spacy"   : local NER — if spaCy tags a PERSON/ORG/GPE span matching a pool
                  entity, resolve to it. Zero API cost. Degrades on virtual /
                  non-Western proper names, which is exactly when the cascade
                  escalates — a demonstration of cascade value, not a bug. Skipped
                  entirely when spaCy is not installed.
      "llm"     : the LLM resolves the mention against the pool. Reuses the
                  discovery service's chat client to keep dependencies few.

    Confidence is structural + gold-free: 1.0 for a literal hit, NER span coverage
    for spaCy, else escalate. tau = 0 trusts literal/spacy; tau = 1 always LLM.
    """
    m = (mention or "").strip()
    if not m or not entity_pool:
        return DisambRoute(None, "none", 1.0)

    # tier 0: literal / alias match (skip for a bare pronoun — nothing to match).
    if not _PRONOUN_ONLY.match(m):
        ml = m.casefold()
        for ent in entity_pool:
            el = ent.casefold()
            if ml == el or ml in el or el in ml:
                return DisambRoute(ent, "literal", 1.0)

    # tier 1: spaCy NER on the mention's context — resolve a named span to a pool
    # entity by literal overlap. Confidence = matched-span fraction. Skipped if
    # spaCy is unavailable.
    if tau > 0.0:
        nlp = _spacy_nlp()
        if nlp is not None:
            doc = nlp(context or m)
            pool_lc = {e.casefold(): e for e in entity_pool}
            for span in doc.ents:
                if span.label_ not in ("PERSON", "ORG", "GPE"):
                    continue
                sl = span.text.casefold()
                hit = pool_lc.get(sl) or next(
                    (orig for lc, orig in pool_lc.items() if sl in lc or lc in sl),
                    None,
                )
                if hit:
                    conf = min(len(span.text), len(hit)) / max(len(span.text), len(hit))
                    if conf >= tau:
                        return DisambRoute(hit, "spacy", conf)

    # tier 2: LLM disambiguation over the pool.
    cands = [CandidateFact(id=uuid.uuid4(), text=e) for e in entity_pool]
    by_id = {c.id: c.text for c in cands}
    picked = await llm.discover_sources(
        f"Which entity does '{m}' refer to, in context: {context}", cands
    )
    if picked:
        return DisambRoute(by_id.get(picked[0]), "llm", 0.0)
    return DisambRoute(None, "llm", 0.0)
