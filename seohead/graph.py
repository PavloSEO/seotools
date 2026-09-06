"""Neutral stored-graph contracts shared by storage and SF analysis layers."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class AnchorGroup:
    source_url: str
    occurrences_count: int
    generic_links: list[dict[str, Any]]
    locations: list[dict[str, Any]]


class ScoreView(Protocol):
    count: int
    median: float

    def score_for(self, normalized_url: str) -> float | None: ...


@dataclass(frozen=True)
class InlinkCompositionRow:
    destination_key: str
    occurrences_count: int
    all_nofollow: bool
    has_known_source: bool
    has_indexable_source: bool
    source_examples: list[str]


class PathSession(Protocol):
    def path_to(self, target: str) -> tuple[str, ...] | None: ...


class GraphAccess(Protocol):
    """Check-independent graph facts; SF retains every threshold and emission."""

    has_resource_type: bool

    @property
    def has_internal_hyperlinks(self) -> bool: ...

    def iter_anchor_groups(
        self, is_generic_anchor: Callable[[str], bool], max_locations: int
    ) -> Iterator[AnchorGroup]: ...

    def link_score(
        self, *, damping: float, max_iterations: int, tolerance: float
    ) -> ScoreView | None: ...

    def iter_inlink_composition(
        self,
        is_indexable_source: Callable[[str], bool | None],
        max_source_examples: int,
    ) -> Iterator[InlinkCompositionRow]: ...

    def begin_paths(self, seed: str) -> PathSession | None: ...

    def iter_resources(self) -> Iterator[tuple[str, str, str]]: ...
