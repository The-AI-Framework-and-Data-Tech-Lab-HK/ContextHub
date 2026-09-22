"""MEME's own judge prompts, transcribed verbatim, for a parity reading of `Abs`.

Why this exists.  Our primary `Abs` criterion (:mod:`abs_judge`) asks for three
things at once -- abstain, cite the withheld value, name the changed upstream --
but MEME's published criterion asks for **one**: does the agent express
uncertainty (Figure 24, p24).  Requirements 2 and 3 are ours, not the paper's, so
numbers scored the three-part way cannot be set beside the paper's.  This module
implements the paper's criterion so both readings can be reported side by side.

It is a second reading of the same answers, not a replacement: no generation is
re-run, and :mod:`abs_judge` is untouched.

Verbatim, and mechanically checked.  Both templates below are copied from the
paper and are asserted to appear *verbatim* in ``public/MEME/meme-paper-fulltext.txt``
(whitespace-flattened, since ``pdftotext -layout`` rewraps sentences).  That test
is the guardrail against tuning a judge prompt until the score looks good: the
prompt is not merely frozen at whatever we first typed, it is pinned to the
paper's own bytes.  The transcription keeps the PDF's typography (en dashes,
arrows, curly quotes) rather than guessing at the ASCII of MEME's unreleased
source -- see ``KNOWN_TRANSCRIPTION_LIMITATION``.

The output contract also differs from our own judge: the paper asks for JSON
(``{"correct": ..., "reason": ...}``), whereas :func:`judge._llm_match` asks for
a bare CORRECT/INCORRECT word.  An unparseable reply is recorded as a parse
failure and scored wrong; it never falls back to string containment, because a
silent fallback to a *different* criterion is exactly what a parity measurement
cannot afford.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

# --- the paper's prompts, verbatim ---------------------------------------------

# Figure 24 (p24): Judge prompt: Absence. The criterion for the off/on arms.
ABS_JUDGE_PROMPT = (
    "You are a judge evaluating whether an AI agent appropriately handles "
    "UNCERTAINTY after an upstream entity changed.\n"
    "QUESTION: {question}\n"
    "GOLD: {gold}\n"
    "AGENT: {agent_answer}\n"
    "The upstream entity changed, making the previous answer potentially "
    "invalid. The agent should express uncertainty.\n"
    "Rules:\n"
    "– Agent says “I don’t know” / “not sure” / “Unknown” / “None” / expresses "
    "uncertainty → YES\n"
    "– Agent acknowledges the upstream change and questions validity → YES\n"
    "– Agent confidently states the OLD value as definitive current fact → NO\n"
    "– Agent provides the old value with no hedging or uncertainty → NO\n"
    "Output ONLY JSON: {“correct”: true/false, “reason”: “one sentence”}"
)

# Figure 18 (p22): Judge prompt: Before-phase (common). MEME uses this for the
# pre-change question of every task type, so the trivial-pass check must use it
# rather than the Absence prompt: before the change there is nothing stale, and
# the gold is the plain value.
BEFORE_JUDGE_PROMPT = (
    "You are a judge evaluating whether an AI agent’s answer is semantically "
    "correct.\n"
    "QUESTION: {question}\n"
    "GOLD: {gold}\n"
    "AGENT: {agent_answer}\n"
    "Does the agent’s answer contain the correct information matching the gold "
    "answer?\n"
    "Rules:\n"
    "– Focus on semantic equivalence, not exact wording\n"
    "– If the gold value is present in the agent’s answer, it is correct — "
    "regardless of any additional information, future possibilities, or extra "
    "details the agent mentions\n"
    "– “Dentist every 6 months; dermatologist monthly (if you change "
    "residence)” → gold is “dentist (every 6 months)” → YES (core answer "
    "correct, extra info is irrelevant)\n"
    "– “40 minutes” = “40 min” → YES\n"
    "– “dentist appointment every 6 months” = “dentist (every 6 months)” → YES\n"
    "– If agent says “I don’t know” and gold is a specific value → NO\n"
    "Output ONLY JSON: {“correct”: true/false, “reason”: “one sentence”}"
)

KNOWN_TRANSCRIPTION_LIMITATION = (
    "Transcribed from the paper PDF, so typographic characters (– for a bullet "
    "hyphen, → for ->, “ ” ’ for ASCII quotes, — for an em dash) are the PDF's "
    "rendering. MEME's source code is not released, so its exact ASCII cannot be "
    "recovered; we reproduce what the paper prints rather than guess."
)

# The paper pins its judge to GPT-4o at temperature 0 (D.5, p22).
JUDGE_TEMPERATURE = 0.0
# Room for the JSON object plus its one-sentence reason. Our own judge caps at 4
# tokens because it wants a single word; that cap would truncate this contract.
JUDGE_MAX_TOKENS = 200

_PLACEHOLDER = re.compile(r"\{(question|gold|agent_answer)\}")


def render(template: str, *, question: str, gold: str, agent_answer: str) -> str:
    """Fill a judge template's three placeholders.

    ``str.format`` cannot be used: the templates end with a literal JSON object,
    so their braces are data, not fields. Substitution is a single regex pass so
    a value that itself contains ``{gold}`` cannot be re-substituted.
    """
    values = {
        "question": question or "",
        "gold": gold or "",
        "agent_answer": agent_answer or "",
    }
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], template)


def flatten(text: str) -> str:
    """Collapse all whitespace runs to single spaces, for paper comparison."""
    return re.sub(r"\s+", " ", text).strip()


def appears_verbatim_in_paper(template: str, paper_text: str) -> bool:
    """True iff the template is a verbatim span of the paper (modulo wrapping)."""
    return flatten(template) in flatten(paper_text)


# --- parsing the paper's JSON verdict ------------------------------------------

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)
# GPT-4o answers this contract in ASCII, but the prompt itself shows curly
# quotes, so a model that mirrors the prompt's typography must still parse.
_SMART_QUOTES = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"})


@dataclass(frozen=True)
class OfficialVerdict:
    correct: bool           # scored result; False when the reply did not parse
    reason: str             # the judge's one sentence, or "" when unparseable
    parse_failed: bool      # reply did not yield {"correct": bool}
    raw_output: str


def parse_verdict(raw_output: str) -> OfficialVerdict:
    """Read the paper's ``{"correct": ..., "reason": ...}`` reply.

    Fail-closed: an unparseable reply scores wrong AND sets ``parse_failed`` so
    the run can report how many verdicts were not really the judge's. It does
    not fall back to containment -- mixing criteria would void the parity claim.
    """
    text = (raw_output or "").strip()
    # Models often wrap JSON in ```json fences despite "Output ONLY JSON".
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    match = _JSON_OBJECT.search(text)
    if match:
        for candidate in (match.group(0), match.group(0).translate(_SMART_QUOTES)):
            try:
                payload = json.loads(candidate)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(payload, dict) and isinstance(payload.get("correct"), bool):
                reason = payload.get("reason")
                return OfficialVerdict(
                    correct=payload["correct"],
                    reason=str(reason) if reason is not None else "",
                    parse_failed=False,
                    raw_output=raw_output or "",
                )
    return OfficialVerdict(
        correct=False, reason="", parse_failed=True, raw_output=raw_output or ""
    )


# --- the case-level criterion --------------------------------------------------

# MEME's trivial-pass rule (p6): an after-answer only counts when the model also
# answered the before-question correctly, so a system that abstains
# unconditionally cannot score. `raw_*` keeps the ungated reading visible.
STAGES = ("before", "off", "on")


def prompt_for_stage(stage: str) -> str:
    """Figure 18 for the pre-change question, Figure 24 for the two after arms."""
    if stage == "before":
        return BEFORE_JUDGE_PROMPT
    if stage in ("off", "on"):
        return ABS_JUDGE_PROMPT
    raise ValueError(f"unknown stage: {stage!r}")


def score_case(verdicts: Mapping[str, OfficialVerdict]) -> dict[str, object]:
    """Per-stage results plus the trivial-pass-gated numbers for one episode."""
    before_ok = bool(verdicts["before"].correct) if "before" in verdicts else False
    row: dict[str, object] = {
        "criterion": "meme_official",
        "stages": {
            stage: {
                "correct": verdicts[stage].correct,
                "reason": verdicts[stage].reason,
                "parse_failed": verdicts[stage].parse_failed,
            }
            for stage in STAGES
            if stage in verdicts
        },
        "before_ok": before_ok,
        "parse_failures": sorted(s for s in verdicts if verdicts[s].parse_failed),
    }
    for arm in ("off", "on"):
        if arm not in verdicts:
            continue
        raw_ok = bool(verdicts[arm].correct)
        row[f"raw_{arm}_ok"] = raw_ok
        row[f"trivial_pass_{arm}"] = raw_ok and before_ok
    return row


def paper_text_path() -> Path:
    """The paper TXT, which lives in the workspace root beside the repo.

    ``parents[3]`` is that root: this file sits at
    ContextHub/integrations/memebench/, so parents[2] is the repo itself and the
    MEME corpus is one level above it (as with meme_filler32k.json).
    """
    return (
        Path(__file__).resolve().parents[3]
        / "public" / "MEME" / "meme-paper-fulltext.txt"
    )
