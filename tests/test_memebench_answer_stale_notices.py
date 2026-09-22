"""Answer-side plumbing for withheld-note explanations (working-notes step 3).

The system already knows why a node was withheld; these tests pin that the
answer prompt is told, that the withheld value never enters the served notes,
and that the default arm's frozen prompt is untouched byte-for-byte.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from contexthub.models.search import SearchResponse, SearchResult, StaleNotice
from integrations.memebench.answer import (
    build_answer_prompt,
    _ANSWER_PROMPT,
    _ANSWER_PROMPT_WITH_NOTICES,
    _entity_from_uri,
    answer_question,
    format_stale_notices,
)

# The default arm's prompt hash is part of run identity for every arm already on
# disk (run_full100_v3_p2 stamps prompt_versions.answer_sha256).  If this value
# has to change, every prior run's identity stops verifying.  Taken from the
# runs/ artifacts themselves, where 323 recorded copies all agree.
_FROZEN_ANSWER_PROMPT_SHA256 = (
    "39b9a942db7e6a5c0e9e4bea21d769d488f76179a6d25ff10bb0d472d247d582"
)


_ABS_GOLD = re.compile(r"^Uncertain — previously '(.*)', but (.+) changed")
_MEME_PATH = (
    Path(__file__).resolve().parents[2] / "public" / "MEME" / "meme_filler32k.json"
)
_MEME_DATA = json.loads(_MEME_PATH.read_text()) if _MEME_PATH.exists() else None


class _RecordingChat:
    def __init__(self, reply: str = "Sunrise Gym"):
        self.reply = reply
        self.prompts: list[str] = []

    async def complete(self, prompt, max_tokens=None):
        self.prompts.append(prompt)
        return self.reply


class _StubRetrieval:
    def __init__(self, response: SearchResponse):
        self._response = response
        self.requests = []

    async def search(self, db, request, ctx):
        self.requests.append(request)
        return self._response


class _StubSystem:
    def __init__(self, response, reply="Sunrise Gym"):
        self.retrieval = _StubRetrieval(response)
        self.answer_chat = _RecordingChat(reply)


def _result(uri: str, text: str) -> SearchResult:
    return SearchResult(
        uri=uri,
        context_type="memory",
        scope="agent",
        score=0.9,
        l2_content=text,
        status="active",
        version=1,
        validity_status="fresh",
    )


def _notice() -> StaleNotice:
    return StaleNotice(
        uri="ctx://agent/eval/memories/cur-fitness_facility-aaa111",
        validity_status="stale",
        reason="health_condition: lactose intolerance -> high blood pressure",
        stale_content="The user works out at Sunrise Gym.",
        version=4,
        source_uri="ctx://agent/eval/memories/cur-work_location-bbb222",
        source_content="The user works at the Portland office.",
    )


def _response(results=(), notices=()) -> SearchResponse:
    return SearchResponse(
        results=list(results),
        total=len(results),
        retrieval_id="00000000-0000-0000-0000-000000000001",
        stale_notices=list(notices),
    )


def test_default_answer_prompt_is_byte_frozen():
    assert (
        hashlib.sha256(_ANSWER_PROMPT.encode()).hexdigest()
        == _FROZEN_ANSWER_PROMPT_SHA256
    )


def test_entity_name_comes_from_the_uri_slug():
    assert _entity_from_uri("ctx://a/b/memories/cur-work_location-bbb222") == (
        "work_location"
    )
    assert _entity_from_uri("ctx://a/b/memories/root-health_condition-c1") == (
        "health_condition"
    )
    # Slugs without an entity segment yield nothing rather than a guess.
    assert _entity_from_uri("ctx://a/b/memories/filler-abc123") is None
    assert _entity_from_uri("bare") is None


def test_every_meme_upstream_entity_survives_the_uri_roundtrip():
    """The 15 upstream names MEME's Abs gold points at are all URI-recoverable.

    ``_entity_from_uri`` is what lets a notice say "work_location changed" rather
    than quoting a uuid.  If any real entity name failed to round-trip through
    the slug ingest writes, the notice would silently degrade to a URI.
    """
    data = _MEME_DATA
    if data is None:
        pytest.skip("MEME corpus not present")
    names = set()
    for episode in data:
        after = episode["after_questions"]
        questions = after["questions"] if isinstance(after, dict) else after
        for question in questions:
            if question.get("task_type") != "Abs":
                continue
            match = _ABS_GOLD.match(question.get("gold_answer") or "")
            if match:
                names.add(match.group(2))
    assert len(names) == 15
    for name in names:
        uri = f"ctx://agent/eval/memories/cur-{name}-abc123"
        assert _entity_from_uri(uri) == name


def test_notice_lines_name_the_prior_value_and_the_upstream():
    rendered = format_stale_notices([_notice()])
    assert "The user works out at Sunrise Gym." in rendered
    assert "work_location" in rendered
    assert "The user works at the Portland office." in rendered


def test_notice_line_falls_back_to_recorded_reason_without_upstream_content():
    notice = _notice()
    notice.source_content = None
    rendered = format_stale_notices([notice])
    assert "reason recorded: health_condition" in rendered


@pytest.mark.asyncio
async def test_explicit_off_reproduces_the_frozen_prompt_byte_for_byte():
    """The off arm: withhold the node, say nothing about why.

    This is how an arm frozen before notices existed is reproduced, and how the
    contribution of the explanation itself is measured. The withheld value must
    not reach the model on this path.
    """
    system = _StubSystem(_response([_result("fresh", "A fresh note.")], [_notice()]))
    result = await answer_question(
        system,
        object(),
        "acme",
        "Where do I work out?",
        with_stale_notices=False,
    )

    assert system.retrieval.requests[0].include_stale_notices is False
    prompt = system.answer_chat.prompts[0]
    assert prompt == _ANSWER_PROMPT.format(
        notes="- A fresh note.", question="Where do I work out?"
    )
    assert "Sunrise Gym" not in prompt
    # The notices still come back on the result for auditing; they just are not
    # put in front of the model.
    assert result.stale_notices == [_notice()]


@pytest.mark.asyncio
async def test_explaining_is_on_by_default():
    system = _StubSystem(_response([_result("fresh", "A fresh note.")], [_notice()]))
    await answer_question(system, object(), "acme", "Where do I work out?")
    assert system.retrieval.requests[0].include_stale_notices is True
    assert "Uncertain — previously" in system.answer_chat.prompts[0]


def test_explaining_withheld_notes_is_universal_not_per_task_type():
    """Withholding a note and saying why is one retrieval behaviour, on by default.

    Conditioning it on task type would tune the system to MEME's answer keys
    rather than build one; the switch lives in run identity so the off arm is a
    measurable comparison under the same source tree, not a special case.
    """
    from integrations.memebench.run_full100_v3_p2 import (
        identity_task_type,
        identity_with_stale_notices,
    )

    for task_type in ("Cas", "Abs"):
        identity = {"config": {"task_type": task_type, "with_stale_notices": True}}
        assert identity_with_stale_notices(identity) is True
        assert identity_task_type(identity) == task_type

    off = {"config": {"task_type": "Cas", "with_stale_notices": False}}
    assert identity_with_stale_notices(off) is False
    # Identities frozen before either key existed: Cas, and never explained.
    assert identity_task_type({"config": {}}) == "Cas"
    assert identity_with_stale_notices({"config": {}}) is False


def test_both_arms_request_notices_so_the_difference_stays_the_staling():
    """ON and OFF must share the prompt rule, or the gap mixes two variables.

    In the OFF arm nothing is stale, so no notices come back and the frozen
    template is used anyway -- same bytes as before, now by rule rather than by
    a separate code path.
    """
    assert build_answer_prompt("- A note.", "Q?", []) == _ANSWER_PROMPT.format(
        notes="- A note.", question="Q?"
    )
    with_notice = build_answer_prompt("- A note.", "Q?", [_notice()])
    assert with_notice != _ANSWER_PROMPT.format(notes="- A note.", question="Q?")
    assert "Uncertain — previously" in with_notice


def test_notices_prompt_does_not_contradict_itself_on_answer_length():
    """The stop point found before the first paid Abs run.

    The first draft opened with "shortest possible span, no explanation" and then
    demanded a full ``Uncertain — previously '...', but ... changed`` sentence.
    The span instruction now applies only to the answerable-from-notes branch.
    """
    header = _ANSWER_PROMPT_WITH_NOTICES.split("Notes:", 1)[0]
    assert "shortest possible span" not in header
    answerable, withheld = _ANSWER_PROMPT_WITH_NOTICES.split(
        "If the answer would have come from a withheld note", 1
    )
    assert "shortest possible span" in answerable
    # The withheld branch must say the value alone is wanted, not the whole
    # sentence the notice rendered, and must show one worked example.
    assert "not the whole sentence" in withheld
    # The frozen-v3 graph carries no entity labels, so the upstream often has no
    # name to quote; the instruction must cover that case rather than assume one
    # is always given.
    assert "when\none is given above; otherwise" in withheld
    assert "on one line" in withheld
    assert "Example: Uncertain — previously 'Sunrise Gym', but work_location changed" in (
        withheld
    )


def test_notices_prompt_is_byte_frozen():
    """Pinned like the default prompt: its hash goes into run identity too."""
    assert (
        hashlib.sha256(_ANSWER_PROMPT_WITH_NOTICES.encode()).hexdigest()
        == "a828caa725bb4d2e9227940495feca20b0032aad64ee23a18641e3052446bcae"
    )


@pytest.mark.asyncio
async def test_notices_on_adds_the_explanation_and_the_uncertain_form():
    system = _StubSystem(
        _response([_result("fresh", "A fresh note.")], [_notice()]),
        reply="Uncertain — previously 'Sunrise Gym', but work_location changed",
    )
    result = await answer_question(
        system,
        object(),
        "acme",
        "Where do I work out?",
        with_stale_notices=True,
    )

    request = system.retrieval.requests[0]
    assert request.include_stale_notices is True
    # Retrieval config is otherwise unchanged: stale nodes are still not served.
    assert request.include_stale is False

    prompt = system.answer_chat.prompts[0]
    assert prompt.startswith(_ANSWER_PROMPT_WITH_NOTICES.split("{notes}")[0])
    assert "- A fresh note." in prompt
    assert "The user works out at Sunrise Gym." in prompt
    assert "work_location" in prompt
    assert "Uncertain — previously" in prompt
    assert result.answer.startswith("Uncertain — previously 'Sunrise Gym'")


@pytest.mark.asyncio
async def test_notices_on_with_nothing_withheld_uses_the_frozen_prompt():
    system = _StubSystem(_response([_result("fresh", "A fresh note.")], []))
    await answer_question(
        system, object(), "acme", "Where do I work out?", with_stale_notices=True
    )
    assert system.answer_chat.prompts[0] == _ANSWER_PROMPT.format(
        notes="- A fresh note.", question="Where do I work out?"
    )


@pytest.mark.asyncio
async def test_withheld_value_never_enters_the_served_notes_block():
    """The prior value appears only under the withheld framing, never as a note."""
    system = _StubSystem(_response([], [_notice()]))
    await answer_question(
        system, object(), "acme", "Where do I work out?", with_stale_notices=True
    )
    prompt = system.answer_chat.prompts[0]
    notes_block = prompt.split("Notes:", 1)[1].split("Some notes were withheld", 1)[0]
    assert "Sunrise Gym" not in notes_block
    assert "(no notes found)" in notes_block


def test_frozen_v3_node_uris_yield_no_entity_name():
    """The formal runner imports the frozen graph as `<episode>-node-<sha256>`
    (run_full100_v3_p2.import_frozen_graph), whose middle slug segment is the
    literal word "node". Parsing that as an entity made every notice say "the
    upstream that changed is: node", which is what the byte-frozen roundtrip test
    above missed by only ever checking gold-facts slugs."""
    frozen = "ctx://agent/meme-full100/memories/pl_001-node-" + "a" * 64
    assert _entity_from_uri(frozen) is None
    # The gold-facts path inserts these three with the role as the whole slug
    # (`ingest._insert_memory(..., "fact", ...)`), so they have no entity segment.
    for role in ("fact", "filler", "change"):
        assert _entity_from_uri(f"ctx://a/b/memories/{role}-abc123") is None


def test_unnamed_upstream_is_described_by_content_not_by_uri():
    """With no entity name, the notice must not print the content-hash URI: it
    reads as if the hash were the upstream's name, and gives the model nothing to
    put in the "but <upstream> changed" slot. The "now says" clause carries the
    same information usefully, and its wording must not dangle a "that upstream"
    with no antecedent."""
    notice = _notice()
    notice.source_uri = "ctx://agent/meme-full100/memories/pl_001-node-" + "a" * 64
    rendered = format_stale_notices([notice])
    assert "a" * 64 not in rendered
    assert "the upstream that changed is:" not in rendered
    assert "that upstream now says" not in rendered
    assert "the upstream that changed now says:" in rendered
    # A named upstream keeps the original two-clause wording.
    named = format_stale_notices([_notice()])
    assert "the upstream that changed is: work_location" in named
    assert "that upstream now says:" in named
