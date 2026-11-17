"""P4-only adapter. Benchmark metadata never crosses the guard boundary."""
from __future__ import annotations

import hashlib
from contextlib import nullcontext
from typing import Any

from .interface import Evidence, MemoryTransition, UpdateContext, Updater


def p4_transition(
    case: dict[str, Any], selected_ids: list[str], proposed_memory: str,
    model: Any, max_words: int,
) -> tuple[MemoryTransition, Updater]:
    # Import lazily: core owns the original updater; the guard does not own one.
    from memform.eval.core import build_batch_memory

    if case.get("interface") != "P4" or case.get("task_format") == "free_form":
        raise ValueError("TransitionGuard MVP supports P4 MCQ only")
    raw = case["candidate_experiences"]
    # Opaque handles and content-derived presentation order avoid E4/E7 labels and
    # candidate-position clues. No supports/scope/provenance/success fields survive.
    ordered = sorted(raw, key=lambda e: hashlib.sha256(str(e["text"]).encode()).hexdigest())
    mapping = {str(e["id"]): Evidence(f"record_{i:03d}", str(e["text"]))
               for i, e in enumerate(ordered, 1)}
    reverse = {e.record_id: original_id for original_id, e in mapping.items()}
    context = UpdateContext(str(case["decision_variable"]),
                            str(case["memory_building"]["batch_prompt"]), max_words)
    transition = MemoryTransition(
        None, tuple(mapping.values()), tuple(mapping[eid] for eid in selected_ids),
        proposed_memory, context,
    )

    def replay(experiences: tuple[Evidence, ...]) -> str:
        if any(mapping.get(reverse.get(e.record_id, "")) != e for e in experiences):
            raise ValueError("Replay may only use unmodified runtime evidence")
        ids = [reverse[e.record_id] for e in experiences]
        if len(ids) != len(set(ids)):
            raise ValueError("Replay must not duplicate an observation")
        if len(ids) == len(raw) and set(ids) == set(mapping):
            # Match MemForm's existing full-pool diagnostic presentation order.
            ids = list(case["memory_building"]["mechanism_controls"]["full_pool"]["experience_ids"])
        scope = model.stage("counterfactual_replay") if hasattr(model, "stage") else nullcontext()
        with scope:
            return build_batch_memory(model, case, ids, context.batch_prompt, max_words)

    return transition, replay
