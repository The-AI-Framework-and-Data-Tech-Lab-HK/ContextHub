"""MEME's published judge criterion, pinned to the paper's own bytes.

The load-bearing tests are the first two: they assert each prompt is a *verbatim
span of the paper text*, not merely unchanged since someone typed it. A frozen
sha256 alone would still let a prompt be tuned once and then frozen at the tuned
wording; pinning to the paper makes "reworded until the score improved"
mechanically impossible. The sha256 tests exist on top of that, to stamp run
identity and to make any edit show up as a diff rather than a silent drift.

Zero API: every test is string work against the paper TXT.
"""

from __future__ import annotations

import hashlib

import pytest

from integrations.memebench.meme_official_judge import (
    ABS_JUDGE_PROMPT,
    BEFORE_JUDGE_PROMPT,
    JUDGE_MAX_TOKENS,
    JUDGE_TEMPERATURE,
    STAGES,
    appears_verbatim_in_paper,
    flatten,
    parse_verdict,
    paper_text_path,
    prompt_for_stage,
    render,
    score_case,
)

# Stamped into every rejudge run's identity. Changing either value invalidates
# the runs already on disk, so a diff here must be deliberate.
_FROZEN_ABS_JUDGE_SHA256 = (
    "7664b362d2910f77636c482f199a406de67563ef0f218a9071dd5ffe9ff8625f"
)
_FROZEN_BEFORE_JUDGE_SHA256 = (
    "bc2de8ba91750092ec6b7def329d77c8c7abb20995b18b507cf95a360ad14517"
)

_PAPER = paper_text_path()
_PAPER_TEXT = _PAPER.read_text(encoding="utf-8") if _PAPER.exists() else None
_needs_paper = pytest.mark.skipif(
    _PAPER_TEXT is None, reason="MEME paper TXT not present"
)


@_needs_paper
def test_abs_judge_prompt_is_verbatim_from_the_paper():
    """Figure 24 (p24), the Absence criterion, copied not paraphrased."""
    assert appears_verbatim_in_paper(ABS_JUDGE_PROMPT, _PAPER_TEXT)


@_needs_paper
def test_before_judge_prompt_is_verbatim_from_the_paper():
    """Figure 18 (p22), the shared Before-phase criterion for trivial-pass."""
    assert appears_verbatim_in_paper(BEFORE_JUDGE_PROMPT, _PAPER_TEXT)


def test_judge_prompts_are_byte_frozen():
    assert hashlib.sha256(ABS_JUDGE_PROMPT.encode()).hexdigest() == (
        _FROZEN_ABS_JUDGE_SHA256
    )
    assert hashlib.sha256(BEFORE_JUDGE_PROMPT.encode()).hexdigest() == (
        _FROZEN_BEFORE_JUDGE_SHA256
    )


@_needs_paper
def test_a_reworded_prompt_would_fail_the_paper_check():
    """The guardrail actually bites: one substantive edit breaks verbatim-ness."""
    tuned = ABS_JUDGE_PROMPT.replace(
        "The agent should express uncertainty.",
        "The agent should express uncertainty, or name the changed upstream.",
    )
    assert tuned != ABS_JUDGE_PROMPT
    assert not appears_verbatim_in_paper(tuned, _PAPER_TEXT)


def test_before_stage_uses_figure_18_and_after_arms_use_figure_24():
    """The before question asks for a plain value; judging it with the Absence
    prompt would score a correct value as a failure to hedge."""
    assert prompt_for_stage("before") is BEFORE_JUDGE_PROMPT
    assert prompt_for_stage("off") is ABS_JUDGE_PROMPT
    assert prompt_for_stage("on") is ABS_JUDGE_PROMPT
    with pytest.raises(ValueError):
        prompt_for_stage("during")


def test_paper_pins_gpt4o_temperature_zero():
    assert JUDGE_TEMPERATURE == 0.0
    # Our own judge caps at 4 tokens for a one-word reply; the paper's contract
    # is a JSON object with a sentence, which that cap would truncate.
    assert JUDGE_MAX_TOKENS > 4


# --- rendering -----------------------------------------------------------------


def test_render_fills_the_three_placeholders_and_keeps_the_json_braces():
    out = render(
        ABS_JUDGE_PROMPT, question="Where do I work?", gold="G", agent_answer="A"
    )
    assert "QUESTION: Where do I work?" in out
    assert "GOLD: G" in out
    assert "AGENT: A" in out
    # The literal JSON contract at the end must survive substitution: str.format
    # would have raised on these braces.
    assert out.rstrip().endswith(
        "Output ONLY JSON: {“correct”: true/false, “reason”: “one sentence”}"
    )
    assert "{question}" not in out


def test_render_does_not_re_substitute_a_value_containing_a_placeholder():
    out = render(
        ABS_JUDGE_PROMPT, question="{gold}", gold="secret", agent_answer="A"
    )
    assert "QUESTION: {gold}" in out
    assert "QUESTION: secret" not in out


def test_render_treats_an_empty_answer_as_empty_not_the_word_none():
    """One hop1 episode really did answer with an empty string; the judge must
    see an empty AGENT line, not the literal text "None"."""
    out = render(ABS_JUDGE_PROMPT, question="Q", gold="G", agent_answer="")
    assert "AGENT: \n" in out


# --- the paper's JSON verdict contract -----------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('{"correct": true, "reason": "hedged appropriately"}', True),
        ('{"correct": false, "reason": "stated the old value"}', False),
        # Models commonly fence JSON despite "Output ONLY JSON".
        ('```json\n{"correct": true, "reason": "unsure"}\n```', True),
        # A model mirroring the prompt's own typography must still parse.
        ('{“correct”: true, “reason”: “unsure”}', True),
        # Prose around the object is tolerated; the object is what counts.
        ('Here is my verdict: {"correct": false, "reason": "definitive"}', False),
    ],
)
def test_parse_verdict_reads_the_papers_contract(raw, expected):
    verdict = parse_verdict(raw)
    assert verdict.correct is expected
    assert verdict.parse_failed is False
    assert verdict.reason


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "CORRECT",                      # our own judge's contract, not the paper's
        "I cannot determine this.",
        '{"reason": "no verdict field"}',
        '{"correct": "true"}',          # string, not bool
        "{not json at all}",
    ],
)
def test_parse_verdict_fails_closed_without_falling_back_to_containment(raw):
    """An unparseable reply scores wrong AND is flagged, never quietly rescored
    by a different criterion -- a silent fallback would void the parity claim."""
    verdict = parse_verdict(raw)
    assert verdict.correct is False
    assert verdict.parse_failed is True
    assert verdict.raw_output == raw


def test_parse_verdict_keeps_the_raw_reply_for_audit():
    raw = '{"correct": true, "reason": "expresses uncertainty"}'
    assert parse_verdict(raw).raw_output == raw


# --- case scoring / trivial pass -----------------------------------------------


def _verdicts(before: bool, off: bool, on: bool):
    return {
        stage: parse_verdict(
            '{"correct": %s, "reason": "r"}' % ("true" if ok else "false")
        )
        for stage, ok in zip(STAGES, (before, off, on))
    }


def test_trivial_pass_requires_the_before_answer_too():
    """MEME's trivial-pass rule (p6): a system that abstains unconditionally
    answers the after-question 'correctly' but fails the before-question, so it
    must not score."""
    row = score_case(_verdicts(before=False, off=False, on=True))
    assert row["raw_on_ok"] is True
    assert row["trivial_pass_on"] is False
    assert row["before_ok"] is False

    row = score_case(_verdicts(before=True, off=False, on=True))
    assert row["trivial_pass_on"] is True
    assert row["trivial_pass_off"] is False


def test_score_case_reports_parse_failures_by_stage():
    verdicts = _verdicts(before=True, off=True, on=True)
    verdicts["on"] = parse_verdict("garbage")
    row = score_case(verdicts)
    assert row["parse_failures"] == ["on"]
    assert row["trivial_pass_on"] is False
    assert row["stages"]["on"]["parse_failed"] is True


def test_flatten_collapses_pdf_line_wrapping():
    assert flatten("a\n  b   c\n") == "a b c"
