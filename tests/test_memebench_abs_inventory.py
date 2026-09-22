"""The `Abs` evaluation set, pinned (working-notes step 4, block 2).

130 `Abs` tasks split into four groups, and which group a case lands in decides
whether it is evaluated, excluded, or evaluated-and-expected-to-fail.  Every
number here was counted from the corpus and the frozen P1 v3 graph, so a corpus
bump or a re-freeze that changes the split fails loudly instead of quietly
moving the denominator underneath a published percentage.

Zero API: file reads and graph traversal only.
"""

from __future__ import annotations

import collections
import json
from pathlib import Path

import pytest

from integrations.memebench.abs_judge import parse_abs_gold
from integrations.memebench.run_full100_v3_p2 import (
    DEFAULT_DATA,
    FrozenV3,
    abs_excluded_episode_ids,
    collapse_root_alias_edges,
    evaluation_episode_ids,
    resolve_root_identity,
)

# v3 discovered the edge but P1 missed it: the dependency chain is real in the
# dataset's own fact fields, so these are a graph-recall hole in the system under
# test, not a corpus defect. They stay in the denominator and are expected to
# fail; hop2 results must be reported split by this list.
V3_MISSING_EDGE = (
    "sw_001", "sw_009", "sw_010", "sw_019", "sw_028", "sw_036", "sw_044", "sw_046",
)
# The target fact is never stated in any session, so no extractor can produce the
# node. The only genuine corpus defect among the 130.
NO_TARGET_NODE = ("pl_030",)


def _corpus_available() -> bool:
    return Path(DEFAULT_DATA).exists()


@pytest.fixture(scope="module")
def dataset():
    if not _corpus_available():
        pytest.skip("MEME corpus not present")
    return json.loads(Path(DEFAULT_DATA).read_text())


@pytest.fixture(scope="module")
def frozen():
    try:
        return FrozenV3()
    except FileNotFoundError:
        pytest.skip("frozen v3 run artifacts not present")


def _abs_tasks(dataset, hop=None):
    rows = []
    for episode in dataset:
        for task in episode.get("tasks", []):
            if task.get("type") != "Abs":
                continue
            if hop is not None and task.get("hop") != hop:
                continue
            rows.append((str(episode["episode_id"]), task))
    return sorted(rows, key=lambda row: (row[0], row[1]["target_entities"][0]))


def _root_reachable_nodes(frozen, episode_id):
    """Nodes the root change can reach, under the runner's own strict rules.

    The alias set is `RootIdentity.aliases` -- the nodes `_classify_root_candidate`
    calls `direct`. Mentions inside a reason clause are rejected, which is why a
    loose "any node whose text contains the prior value" count over-counts.
    """
    identity = resolve_root_identity(frozen, episode_id, data=DEFAULT_DATA)
    edges, _ = collapse_root_alias_edges(frozen.selected_edges(episode_id), identity)
    adjacency = collections.defaultdict(set)
    for edge in edges:
        adjacency[str(edge["dependency_node_id"])].add(str(edge["dependent_node_id"]))
    seen = set(identity.alias_node_ids)
    stack = list(seen)
    while stack:
        for nxt in adjacency[stack.pop()]:
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return identity, seen


def test_abs_splits_into_100_hop1_and_30_hop2(dataset):
    hops = collections.Counter(task["hop"] for _, task in _abs_tasks(dataset))
    assert hops == {1: 100, 2: 30}


def test_abs_and_cas_target_entities_never_overlap(dataset):
    """Why artifact paths keyed on hash(target) do not collide across task types.

    Change 2 in the plan renames them anyway -- an implicit assumption made
    explicit before extending -- but this is the reason no prior run was corrupt.
    """
    for episode in dataset:
        abs_targets, cas_targets = set(), set()
        for task in episode.get("tasks", []):
            if task.get("type") == "Abs":
                abs_targets.update(task["target_entities"])
            elif task.get("type") == "Cas":
                cas_targets.update(task.get("target_entities") or [])
        assert not (abs_targets & cas_targets), episode["episode_id"]


def test_strict_alias_sets_are_a_single_node(dataset, frozen):
    """The count that corrected 29 down to 21.

    A loose start set (every node mentioning the prior value) runs 4-5 nodes wide
    and reports reachability the runner would never see.
    """
    sizes = collections.Counter()
    for episode_id, _ in _abs_tasks(dataset, hop=2):
        identity, _ = _root_reachable_nodes(frozen, episode_id)
        sizes[len(identity.aliases)] += 1
    assert sizes == {1: 29, 2: 1}


def test_hop2_splits_21_reachable_8_missing_edge_1_absent(dataset, frozen):
    reachable, missing_edge, absent = [], [], []
    for episode_id, task in _abs_tasks(dataset, hop=2):
        _, seen = _root_reachable_nodes(frozen, episode_id)
        entity = task["target_entities"][0]
        prior = task["entity_values"][entity].casefold()
        target_nodes = {
            str(node["node_id"])
            for node in frozen.episode(episode_id).get("nodes") or ()
            if prior in str(node.get("text") or "").casefold()
        }
        if target_nodes & seen:
            reachable.append(episode_id)
        elif not target_nodes:
            absent.append(episode_id)
        else:
            missing_edge.append(episode_id)

    assert len(reachable) == 21
    assert tuple(sorted(missing_edge)) == V3_MISSING_EDGE
    assert tuple(sorted(absent)) == NO_TARGET_NODE
    # Evaluated = everything except the fact that was never stated.
    assert len(reachable) + len(missing_edge) == 29


def test_the_missing_edge_cases_have_a_real_chain_in_the_corpus(dataset):
    """These 8 are a P1 recall hole, not a corpus defect -- hence in the denominator.

    Each one's dependency chain is present in the dataset's own per-fact
    `dependency_source` fields, so the corpus does assert root -> ... -> target.
    """
    checked = 0
    for episode in dataset:
        episode_id = str(episode["episode_id"])
        if episode_id not in V3_MISSING_EDGE:
            continue
        adjacency = collections.defaultdict(set)
        for session in episode.get("sessions", []):
            for fact in session.get("gold_facts", []):
                if fact.get("dependency_source"):
                    adjacency[str(fact["dependency_source"])].add(str(fact["entity"]))
        seen = {str(episode["root"])}
        stack = list(seen)
        while stack:
            for nxt in adjacency[stack.pop()]:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        targets = [
            task["target_entities"][0]
            for task in episode.get("tasks", [])
            if task.get("type") == "Abs" and task.get("hop") == 2
        ]
        assert targets and targets[0] in seen, episode_id
        checked += 1
    assert checked == len(V3_MISSING_EDGE)


def test_hop2_reachable_domain_skew_is_recorded(dataset, frozen):
    """pl 8 / sw 13 among the reachable, so hop2 conclusions stay stratified."""
    domains = collections.Counter()
    for episode_id, task in _abs_tasks(dataset, hop=2):
        if episode_id in V3_MISSING_EDGE + NO_TARGET_NODE:
            continue
        domains[episode_id[:2]] += 1
    assert domains == {"sw": 13, "pl": 8}


def test_exclusions_are_per_hop_not_per_episode(frozen):
    """An episode excluded at hop1 can still have a scoreable hop2 Abs task.

    Filtering both hops by the flat 11-id set drops 4 hop2 cases and 1 hop1 case
    that were never excluded, silently shrinking the denominator to 89/25.
    """
    assert len(abs_excluded_episode_ids(1)) == 10
    assert abs_excluded_episode_ids(2) == frozenset({"pl_030"})
    assert len(abs_excluded_episode_ids()) == 11  # superset; never use to filter


def test_evaluation_set_is_90_hop1_and_29_hop2(frozen):
    hop1 = evaluation_episode_ids(frozen, evaluation_hop=1, task_type="Abs")
    hop2 = evaluation_episode_ids(frozen, evaluation_hop=2, task_type="Abs")
    assert len(hop1) == 90
    assert len(hop2) == 29
    assert len(hop1) + len(hop2) == 119
    # The missing-edge cases stay in; the never-uttered fact goes out.
    assert set(V3_MISSING_EDGE).issubset(set(hop2))
    assert "pl_030" not in hop2


def test_cas_counts_are_untouched_by_the_abs_exclusions(frozen):
    for hop, expected in ((1, 100), (2, 64)):
        assert len(
            evaluation_episode_ids(frozen, evaluation_hop=hop, task_type="Cas")
        ) == expected


def test_gold_parses_for_every_task_in_the_evaluated_set(dataset):
    """Nothing enters the evaluated set whose gold the scorer cannot read."""
    for episode_id, task in _abs_tasks(dataset):
        if episode_id in NO_TARGET_NODE and task.get("hop") == 2:
            continue
        parsed = parse_abs_gold(task["gold_answer"])
        assert parsed is not None, (episode_id, task["gold_answer"])
        assert parsed.prev_value == task["entity_values"][task["target_entities"][0]]
        assert parsed.upstream == task["cascade_source"]


def test_missing_replacement_is_not_a_failure_layer_for_abs():
    """Abs's premise is that no current value exists.

    The ladder was written for Cas, where a replacement value must be retrieved.
    Keeping that rung for Abs would mark every single case as failing at
    `retrieval-replacement`, hiding whatever actually went wrong. The rungs above
    it still apply: the stale value must be withheld in the ON arm.
    """
    import inspect

    from integrations.memebench.run_full100_v3_p2 import evaluate_runtime_trace

    source = inspect.getsource(evaluate_runtime_trace)
    assert 'task_type == "Abs"' in source
    assert '"retrieval-replacement"' in source
    # The old-value rungs are shared by both task types, never skipped.
    assert '"retrieval-old-on"' in source
    assert '"retrieval-old-off"' in source
