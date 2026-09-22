"""Validated data model for reliability-constrained propagation planning."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from types import MappingProxyType
from typing import Hashable, Mapping

Node = Hashable
EdgeKey = tuple[Node, Node]


def _nonnegative_finite(value: float, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a real number")
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{field_name} must be finite and non-negative")
    return value


def _probability(value: float, field_name: str) -> float:
    value = _nonnegative_finite(value, field_name)
    if value > 1:
        raise ValueError(f"{field_name} must lie in [0, 1]")
    return value


@dataclass(frozen=True, slots=True)
class PolicyCandidate:
    """One policy available for an edge.

    ``delta`` is an upper bound on false-fresh risk and ``cost`` is additive.
    A cascade must be calibrated as one candidate; risks of its component
    models must not be multiplied under an independence assumption.
    """

    name: str
    cost: float
    delta: float

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("candidate name must be a non-empty string")
        object.__setattr__(self, "cost", _nonnegative_finite(self.cost, "cost"))
        object.__setattr__(self, "delta", _probability(self.delta, "delta"))


@dataclass(frozen=True, slots=True)
class EdgeOption:
    """An upstream-to-dependent edge and its finite policy menu."""

    upstream: Node
    dependent: Node
    candidates: tuple[PolicyCandidate, ...]

    def __post_init__(self) -> None:
        if self.upstream == self.dependent:
            raise ValueError("self edges are not allowed in a DAG")
        candidates = tuple(self.candidates)
        if not candidates:
            raise ValueError("every edge must have at least one candidate")
        if not all(isinstance(candidate, PolicyCandidate) for candidate in candidates):
            raise TypeError("edge candidates must be PolicyCandidate instances")
        names = [candidate.name for candidate in candidates]
        if len(names) != len(set(names)):
            raise ValueError("candidate names must be unique within an edge menu")
        object.__setattr__(self, "candidates", candidates)

    @property
    def key(self) -> EdgeKey:
        return (self.upstream, self.dependent)


@dataclass(frozen=True, slots=True)
class GraphProblem:
    """A finite DAG planning instance.

    Edge direction is always ``upstream -> dependent``.  Every protected
    target must be reachable from at least one source.
    """

    nodes: tuple[Node, ...]
    edges: tuple[EdgeOption, ...]
    sources: tuple[Node, ...]
    targets: tuple[Node, ...]
    epsilon: float
    _topological_order: tuple[Node, ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        nodes = tuple(self.nodes)
        edges = tuple(self.edges)
        sources = tuple(self.sources)
        targets = tuple(self.targets)
        object.__setattr__(self, "nodes", nodes)
        object.__setattr__(self, "edges", edges)
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "targets", targets)
        object.__setattr__(self, "epsilon", _probability(self.epsilon, "epsilon"))

        if not nodes:
            raise ValueError("graph must contain at least one node")
        try:
            node_set = set(nodes)
        except TypeError as exc:
            raise TypeError("graph nodes must be hashable") from exc
        if len(node_set) != len(nodes):
            raise ValueError("graph nodes must be unique")
        if not sources or len(set(sources)) != len(sources):
            raise ValueError("sources must be a non-empty unique sequence")
        if not targets or len(set(targets)) != len(targets):
            raise ValueError("targets must be a non-empty unique sequence")
        if not set(sources) <= node_set or not set(targets) <= node_set:
            raise ValueError("all sources and targets must be graph nodes")
        if not all(isinstance(edge, EdgeOption) for edge in edges):
            raise TypeError("edges must be EdgeOption instances")

        edge_keys = [edge.key for edge in edges]
        if len(edge_keys) != len(set(edge_keys)):
            raise ValueError("parallel/duplicate edges are not supported")
        if any(edge.upstream not in node_set or edge.dependent not in node_set for edge in edges):
            raise ValueError("every edge endpoint must be a graph node")

        children: dict[Node, list[Node]] = {node: [] for node in nodes}
        indegree: dict[Node, int] = {node: 0 for node in nodes}
        for edge in edges:
            children[edge.upstream].append(edge.dependent)
            indegree[edge.dependent] += 1

        queue = [node for node in nodes if indegree[node] == 0]
        order: list[Node] = []
        cursor = 0
        while cursor < len(queue):
            node = queue[cursor]
            cursor += 1
            order.append(node)
            for child in children[node]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    queue.append(child)
        if len(order) != len(nodes):
            raise ValueError("graph must be acyclic")
        object.__setattr__(self, "_topological_order", tuple(order))

        reachable = set(sources)
        for node in order:
            if node in reachable:
                reachable.update(children[node])
        missing = set(targets) - reachable
        if missing:
            raise ValueError(f"targets are unreachable from sources: {missing!r}")

    @property
    def topological_order(self) -> tuple[Node, ...]:
        return self._topological_order

    @property
    def edge_map(self) -> Mapping[EdgeKey, EdgeOption]:
        return MappingProxyType({edge.key: edge for edge in self.edges})


@dataclass(frozen=True, slots=True)
class PlanResult:
    """A planner output with independently computed feasibility metadata."""

    assignments: Mapping[EdgeKey, PolicyCandidate]
    total_cost: float
    max_path_risk: float
    feasible: bool
    method: str
    violating_path: tuple[Node, ...] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "assignments", MappingProxyType(dict(self.assignments)))
        object.__setattr__(self, "total_cost", _nonnegative_finite(self.total_cost, "total_cost"))
        object.__setattr__(
            self,
            "max_path_risk",
            _nonnegative_finite(self.max_path_risk, "max_path_risk"),
        )
        if not isinstance(self.feasible, bool):
            raise TypeError("feasible must be a bool")
        if not isinstance(self.method, str) or not self.method:
            raise ValueError("method must be a non-empty string")
        if self.violating_path is not None:
            object.__setattr__(self, "violating_path", tuple(self.violating_path))


class PlanningInfeasibleError(ValueError):
    """Raised when no assignment satisfies every protected path."""

