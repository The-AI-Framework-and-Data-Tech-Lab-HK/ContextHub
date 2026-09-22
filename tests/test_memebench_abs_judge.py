"""Three-part scoring for MEME's `Abs` task (working-notes step 4, block 1).

The load-bearing test here is the first one: it pins *why* this module exists,
by showing the existing containment judge rejecting correct paraphrases of an
`Abs` gold sentence, and giving one bit where three are needed.

Zero API: every test is pure string work against the real corpus.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from integrations.memebench.abs_judge import (
    ABS_GOLD_RE,
    parse_abs_gold,
    score_abs,
    score_abs_gold,
    upstream_variants,
)
from integrations.memebench.judge import matches, normalize

_MEME_PATH = (
    Path(__file__).resolve().parents[2] / "public" / "MEME" / "meme_filler32k.json"
)
_MEME_DATA = json.loads(_MEME_PATH.read_text()) if _MEME_PATH.exists() else None

_GOLD = "Uncertain — previously 'tutoring session (weekly)', but health_condition changed"


def _abs_golds() -> list[str]:
    """Every `Abs` gold in the after-questions blocks (the loader's read path)."""
    golds = []
    for episode in _MEME_DATA:
        after = episode["after_questions"]
        questions = after["questions"] if isinstance(after, dict) else after
        for question in questions:
            if question.get("task_type") == "Abs":
                golds.append(question.get("gold_answer") or "")
    return golds


def test_containment_judge_rejects_correct_paraphrases_of_abs_gold():
    """Why this module exists.

    Containment asks whether the whole normalized gold sentence sits inside the
    answer, so anything short of near-verbatim reproduction fails -- including
    answers that satisfy all three gold requirements. The scorer credits them.
    """
    paraphrases = (
        "I'm not sure — it was previously a tutoring session (weekly), "
        "but your health_condition changed.",
        "Uncertain. Previously tutoring session (weekly); health condition changed.",
    )
    for answer in paraphrases:
        assert matches(answer, _GOLD) is False, answer      # the false negative
        assert score_abs_gold(answer, _GOLD).all_three is True, answer

    # And it collapses three requirements into one bit: both of these are wrong,
    # but containment cannot say that one abstained and the other did not.
    assert matches("tutoring session (weekly)", _GOLD) is False
    assert matches("Uncertain — health_condition changed", _GOLD) is False
    stale_as_current = score_abs_gold("tutoring session (weekly)", _GOLD)
    no_value = score_abs_gold("Uncertain — health_condition changed", _GOLD)
    assert (stale_as_current.abstained, stale_as_current.cited_prev) == (False, True)
    assert (no_value.abstained, no_value.cited_prev) == (True, False)


def test_full_gold_answer_scores_all_three():
    verdict = score_abs_gold(_GOLD, _GOLD)
    assert (verdict.abstained, verdict.cited_prev, verdict.named_upstream) == (
        True, True, True,
    )
    assert verdict.all_three is True


def test_each_part_can_fail_independently():
    # Abstains and names the upstream, but will not say what the value was.
    partial = "Uncertain — health_condition changed, so I can't say."
    verdict = score_abs_gold(partial, _GOLD)
    assert (verdict.abstained, verdict.cited_prev, verdict.named_upstream) == (
        True, False, True,
    )

    # Abstains and cites the value, but never names what changed.
    partial = "I'm not sure — it used to be a tutoring session (weekly)."
    verdict = score_abs_gold(partial, _GOLD)
    assert (verdict.abstained, verdict.cited_prev, verdict.named_upstream) == (
        True, True, False,
    )
    assert verdict.all_three is False


def test_hedging_while_asserting_the_stale_value_is_not_abstention():
    """"Probably still X" commits to X. Credit for abstaining is withheld."""
    verdict = score_abs_gold(
        "Not sure, but it's still a tutoring session (weekly) as far as I know.",
        _GOLD,
    )
    assert verdict.asserts_current is True
    assert verdict.abstained is False
    assert verdict.all_three is False


def test_upstream_is_matched_in_both_snake_case_and_spaced_form():
    """normalize() eats the underscore, so the two spellings differ after it."""
    assert normalize("health_condition") != normalize("health condition")
    variants = upstream_variants("health_condition")
    assert normalize("health_condition") in variants
    assert normalize("health condition") in variants

    for phrasing in ("health_condition changed", "the health condition changed"):
        verdict = score_abs("Uncertain — previously 'x', but " + phrasing, "x", "health_condition")
        assert verdict.named_upstream is True, phrasing


def test_prior_values_with_urls_and_qualifiers_are_matched():
    """Real prior values include URLs, flags and parenthesised qualifiers."""
    for prev in (
        "stream.velturis.io/aurora",
        "hybrid (3 office + 2 remote)",
        "tarvex verify --integration",
        "zyndra://base-main:9042/aurora",
    ):
        answer = f"Uncertain — previously '{prev}', but deploy_target changed"
        verdict = score_abs(answer, prev, "deploy_target")
        assert verdict.all_three is True, prev


def test_unparsable_gold_returns_none_rather_than_scoring_against_nothing():
    assert parse_abs_gold("James Lee") is None
    assert parse_abs_gold("") is None
    assert score_abs_gold("anything", "James Lee") is None


def test_empty_and_missing_answers_score_nothing():
    verdict = score_abs("", "tutoring session", "health_condition")
    assert (verdict.abstained, verdict.cited_prev, verdict.named_upstream) == (
        False, False, False,
    )
    assert score_abs(None, "tutoring session", "health_condition").all_three is False


# --- corpus-wide invariants (the plan's "verified 130/130" claims) -------------


def test_every_abs_gold_parses_and_self_scores():
    if _MEME_DATA is None:
        pytest.skip("MEME corpus not present")
    golds = _abs_golds()
    assert len(golds) == 130
    for gold in golds:
        parsed = parse_abs_gold(gold)
        assert parsed is not None, gold
        # Gold graded against itself must satisfy all three parts, otherwise the
        # criterion is unreachable by construction.
        assert score_abs(gold, parsed.prev_value, parsed.upstream).all_three, gold


def test_prior_value_and_upstream_agree_with_the_structured_fields():
    """The parsed sentence must match the task's own fields, not just look right."""
    if _MEME_DATA is None:
        pytest.skip("MEME corpus not present")
    checked = 0
    for episode in _MEME_DATA:
        tasks = {}
        for task in episode.get("tasks", []):
            if task.get("type") == "Abs":
                tasks[task["target_entities"][0]] = task
        for task in tasks.values():
            parsed = parse_abs_gold(task["gold_answer"])
            entity = task["target_entities"][0]
            assert parsed.prev_value == task["entity_values"][entity]
            assert parsed.upstream == task["cascade_source"]
            checked += 1
    assert checked == 130


def test_no_gold_vocabulary_collides_with_the_marker_word_lists():
    """A prior value containing "still" or "uncertain" would corrupt scoring."""
    if _MEME_DATA is None:
        pytest.skip("MEME corpus not present")
    from integrations.memebench.abs_judge import _ABSTAIN, _ASSERTS_CURRENT

    for gold in _abs_golds():
        parsed = parse_abs_gold(gold)
        vocabulary = f"{parsed.prev_value} {parsed.upstream}".casefold()
        for marker in tuple(_ABSTAIN) + tuple(_ASSERTS_CURRENT):
            assert marker not in vocabulary, (marker, gold)


def test_the_em_dash_is_not_stripped_by_normalize():
    """Documented trap: the em-dash survives normalize(), so matching must not
    assume either its presence or its absence."""
    assert "—" in normalize("Uncertain — previously")
    assert ABS_GOLD_RE.match(_GOLD) is not None


# --- runner-side stage scoring -------------------------------------------------


_STAGE_GOLD = {
    "before": "tutoring session (weekly)",
    "off": _GOLD,
    "on": _GOLD,
}


def test_stage_scoring_separates_the_three_requirements():
    from integrations.memebench.run_full100_v3_p2 import score_abs_stages

    scored = score_abs_stages(
        {"before": "tutoring session (weekly)", "off": _GOLD, "on": _GOLD},
        _STAGE_GOLD,
    )
    assert scored["stages"]["on"]["correct"] is True
    assert scored["trivial_pass"] is True
    # The before stage asks for the plain prior value, so it is judged by
    # containment, not by the abstention criterion.
    assert scored["stages"]["before"]["criterion"] == "containment"
    assert scored["stages"]["on"]["criterion"] == "abs_three_part"


def test_stage_scoring_rejects_the_stale_value_served_as_current():
    from integrations.memebench.run_full100_v3_p2 import score_abs_stages

    scored = score_abs_stages(
        {
            "before": "tutoring session (weekly)",
            "off": "tutoring session (weekly)",
            "on": "tutoring session (weekly)",
        },
        _STAGE_GOLD,
    )
    on = scored["stages"]["on"]
    assert (on["abstained"], on["cited_prev"], on["named_upstream"]) == (
        False, True, False,
    )
    assert on["correct"] is False
    assert scored["trivial_pass"] is False


def test_trivial_pass_blocks_a_model_that_always_abstains():
    """Answering the pre-change question wrongly voids the case (MEME's rule)."""
    from integrations.memebench.run_full100_v3_p2 import score_abs_stages

    scored = score_abs_stages(
        {"before": "yoga 2x/week", "off": _GOLD, "on": _GOLD}, _STAGE_GOLD
    )
    assert scored["stages"]["on"]["correct"] is True
    assert scored["stages"]["before"]["correct"] is False
    assert scored["trivial_pass"] is False


def test_criteria_disagreements_are_recorded_not_silently_resolved():
    from integrations.memebench.run_full100_v3_p2 import score_abs_stages

    scored = score_abs_stages(
        {"before": "tutoring session (weekly)", "on": _GOLD},
        {"before": "tutoring session (weekly)", "on": _GOLD},
        [
            {"stage": "before", "parsed_verdict": "correct"},
            {"stage": "on", "parsed_verdict": "incorrect"},
        ],
    )
    assert scored["stages"]["on"]["correct"] is True
    assert scored["llm_judge_by_stage"]["on"] is False
    assert scored["disagreements"] == ["on"]


def test_unparsable_gold_is_scored_false_not_crashed():
    from integrations.memebench.run_full100_v3_p2 import score_abs_stages

    scored = score_abs_stages({"on": "anything"}, {"on": "James Lee"})
    assert scored["stages"]["on"]["gold_parsed"] is False
    assert scored["stages"]["on"]["correct"] is False
