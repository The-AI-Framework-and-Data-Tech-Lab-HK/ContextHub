"""Deterministic three-part scoring for MEME's `Abs` (abstention) task type.

`Abs` gold is not a value but a three-part sentence::

    Uncertain — previously 'tutoring session (weekly)', but health_condition changed

so a correct answer has to do three things at once: abstain, cite the prior
value, and name the upstream that changed.  ``judge.matches`` cannot score this.
It tests whether the normalized *gold* is contained in the answer, which for a
whole sentence demands near-verbatim reproduction: "Uncertain. Previously
tutoring session (weekly); health condition changed." is a correct answer and
fails containment (word order, and normalize() strips the underscore so
`health_condition` and `health condition` diverge).  Containment also yields one
bit where the analysis needs three -- it cannot say *which* requirement a wrong
answer missed.  Hence a separate scorer; `judge.py` is untouched and keeps
grading `Cas`.

All three labels are mechanically derivable (verified 130/130 against
meme_filler32k.json), so this is the primary criterion: zero API, zero judge
noise.  ``judge_case_async(chat=...)`` still runs alongside as a secondary
reading, and disagreements between the two are reported rather than resolved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from integrations.memebench.judge import normalize

# Gold shape. The dash is an em-dash, which `string.punctuation` does not
# contain, so normalize() leaves it in place -- never rely on its absence.
ABS_GOLD_RE = re.compile(r"^Uncertain\s+—\s+previously\s+'(.*)',\s+but\s+(.+?)\s+changed")

# Abstention markers, matched on the normalized answer. Verified to collide with
# none of the 96 distinct prior values nor the 15 upstream names.
_ABSTAIN = (
    "uncertain", "not certain", "not sure", "unsure", "unclear", "unknown",
    "no longer", "cant be sure", "cannot be sure", "may have changed",
    "might have changed", "out of date", "outdated", "stale",
    "不确定", "不清楚", "无法确定", "可能已变",
)

# Phrases that assert a value as *currently* holding. An answer carrying both an
# abstention marker and one of these is hedging while still committing to the
# stale value, which `Abs` counts as wrong. Also collision-free against the gold
# vocabulary.
_ASSERTS_CURRENT = (
    "still", "currently", "is now", "remains", "unchanged", "continues to",
    "仍然", "目前是", "依然",
)


@dataclass(frozen=True)
class AbsGold:
    prev_value: str
    upstream: str


def parse_abs_gold(gold: str) -> AbsGold | None:
    """Recover (prior value, upstream entity) from an `Abs` gold string.

    Returns None rather than guessing when the shape does not match, so a corpus
    change surfaces as an unparsed count instead of silently scoring against
    empty targets.
    """
    match = ABS_GOLD_RE.match((gold or "").strip())
    if not match:
        return None
    return AbsGold(prev_value=match.group(1), upstream=match.group(2))


def upstream_variants(upstream: str) -> set[str]:
    """Normalized surface forms of an entity name a model might produce.

    Entity names are snake_case (`health_condition`) and normalize() strips the
    underscore as punctuation, yielding `healthcondition` -- which never matches
    a model writing "health condition". Both spellings are accepted.
    """
    raw = (upstream or "").strip()
    forms = {raw, raw.replace("_", " "), raw.replace("_", "-")}
    return {n for n in (normalize(f) for f in forms) if n}


@dataclass
class AbsVerdict:
    abstained: bool          # signalled uncertainty without asserting a current value
    cited_prev: bool         # quoted the withheld prior value
    named_upstream: bool     # named the upstream entity that changed
    asserts_current: bool    # kept for auditing: hedged yet still committed
    all_three: bool          # the reported criterion
    answer: str


def score_abs(answer: str, prev_value: str, upstream: str) -> AbsVerdict:
    """Score one `Abs` answer against its three gold requirements.

    ``prev_value`` and ``upstream`` come from :func:`parse_abs_gold`; both sides
    of every comparison go through the same normalize(), because comparing raw
    text against normalized text is what makes containment checks flaky.
    """
    text = normalize(answer)
    prev = normalize(prev_value)

    asserts_current = any(marker in text for marker in _ASSERTS_CURRENT)
    abstained = any(marker in text for marker in _ABSTAIN) and not asserts_current
    # An empty prev after normalization (a value made only of punctuation) has
    # nothing to verify, so it cannot be credited.
    cited_prev = bool(prev) and prev in text
    named_upstream = any(v in text for v in upstream_variants(upstream))

    return AbsVerdict(
        abstained=abstained,
        cited_prev=cited_prev,
        named_upstream=named_upstream,
        asserts_current=asserts_current,
        all_three=abstained and cited_prev and named_upstream,
        answer=answer,
    )


def score_abs_gold(answer: str, gold: str) -> AbsVerdict | None:
    """Convenience: parse the gold, then score. None when the gold is unparsable."""
    parsed = parse_abs_gold(gold)
    if parsed is None:
        return None
    return score_abs(answer, parsed.prev_value, parsed.upstream)
