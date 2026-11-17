"""The complete allowlisted input surface exposed to a post-consolidation guard."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Literal, Protocol


@dataclass(frozen=True)
class Evidence:
    record_id: str  # anonymous runtime handle, not the benchmark experience ID
    text: str


@dataclass(frozen=True)
class UpdateContext:
    decision_variable: str
    batch_prompt: str
    max_words: int


@dataclass(frozen=True)
class MemoryTransition:
    previous_memory: str | None
    candidate_experiences: tuple[Evidence, ...]
    selected_experiences: tuple[Evidence, ...]
    proposed_memory: str
    update_context: UpdateContext

    def __post_init__(self) -> None:
        pool = {e.record_id: e for e in self.candidate_experiences}
        if len(pool) != len(self.candidate_experiences):
            raise ValueError("Duplicate runtime evidence handles")
        selected = self.selected_experiences
        if len({e.record_id for e in selected}) != len(selected):
            raise ValueError("Duplicate selected evidence")
        if any(pool.get(e.record_id) != e for e in selected):
            raise ValueError("Selected evidence must belong to the candidate pool")

    @property
    def omitted_experiences(self) -> tuple[Evidence, ...]:
        selected = {e.record_id for e in self.selected_experiences}
        return tuple(e for e in self.candidate_experiences if e.record_id not in selected)


Updater = Callable[[tuple[Evidence, ...]], str]
CommitDecision = Literal["commit_original", "commit_repaired", "defer"]


@dataclass
class GuardResult:
    original_memory: str
    final_memory: str
    commit_decision: CommitDecision = "commit_original"
    status: str = "no_gap"
    repaired_memory: str | None = None
    verification_reason: str = ""
    review_triggered: bool = False
    evidence_gap: bool = False
    repair_attempted: bool = False
    repair_abandoned: bool = False
    witnesses: list[dict[str, Any]] = field(default_factory=list)
    trials: list[dict[str, Any]] = field(default_factory=list)
    selection: dict[str, Any] = field(default_factory=dict)
    repair: dict[str, Any] = field(default_factory=dict)
    candidate_pool_size: int = 0
    selected_count: int = 0
    omitted_count: int = 0
    counterfactual_rebuilds: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PostConsolidationGuard(Protocol):
    def review(self, transition: MemoryTransition, updater: Updater) -> GuardResult: ...
