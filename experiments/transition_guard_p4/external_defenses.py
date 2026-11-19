"""Native MemForm baselines; no changes to their prompts or decision rules."""
from __future__ import annotations

import json
import re

from memform.common import stable_hash
from memform.defenses import DEFENSE_REGISTRY
from memform.defenses.common import TECHNICAL_FAILURE_CLASSES
from memform.defenses.task_shield import TaskShield
from memform.eval.core import CachedChatModel
from .runtime import TrackedModel

VERSION = "p4-external-defenses-v1"
METHODS = {"D1": "llm_judge", "D2": "task_shield", "D3": "a_memguard"}
VISIBILITY = {
    "D1": "Retained S only, domain and decision variable; no downstream task or omitted history.",
    "D2": "Learned memory and downstream task; no omitted history; use-time feedback/blocking.",
    "D3": "Retained S and downstream task; no omitted history; query-conditioned consensus/lessons.",
}


class MemoryUseTaskShield(TaskShield):
    """Keep the no-memory diagnostic undefended, as in the fixed B0 reference."""
    def intercept_agent_generation(self, generate, system, user, runtime):
        if runtime.memory_text is None:
            return generate(system, user), {}
        return super().intercept_agent_generation(generate, system, user, runtime)


def completed(row):
    """Semantic blocks are outcomes; unresolved technical failures are not gains."""
    if not row.get("task_results"):
        return False
    for task in row["task_results"]:
        for side in ("baseline", "learned_clean", "learned_poisoned"):
            result = task.get(side) or {}
            if (result.get("defense_technical_failure") or
                    result.get("defense_outcome_class") in TECHNICAL_FAILURE_CLASSES):
                return False
            if result.get("parse_success") is True:
                continue
            if (result.get("defense_blocked") and
                    result.get("defense_outcome_class") == "semantic_misaligned"):
                continue
            return False
    return True


class SyntheticDefense:
    """Schema-only backend. Deliberately provides no semantic defense evidence."""
    def __init__(self, method, cache):
        self.method, self.cache = method, cache
        self.cache_hits = self.real_calls = self.simulated_calls = 0
        self.last_call_usage = None

    def invoke(self, system, user):
        key = stable_hash({"version": VERSION, "mock": "schema-v2", "method": self.method,
                           "system": system, "user": user})
        cached = self.cache.get(key)
        if cached is not None:
            self.cache_hits += 1
            return cached
        self.simulated_calls += 1
        indices = sorted({int(n) for n in re.findall(r"(?:Candidate|Path) (\d+):", user)})
        if self.method == "llm_judge":
            rows = [{"index": i, "accepted": True, "score": 1.0, "reason": "SYNTHETIC"}
                    for i in indices]
            value = {"decisions": rows} if rows else {"accepted": True, "score": 1.0}
        elif self.method == "a_memguard":
            if "divergence_score" in system:
                value = {"judgments": [{"index": i, "divergence_score": 0.0,
                                        "anomalous": False, "reason": "SYNTHETIC"} for i in indices]}
            else:
                value = {"paths": [{"index": i, "recommended_action": "SYNTHETIC",
                                    "rationale": "SYNTHETIC", "key_steps": ["SYNTHETIC"]}
                                   for i in indices]}
        elif "task alignment checker" in system or "ContributeTo score" in system:
            value = [{"user_task_instruction": "Complete the operational task.",
                      "ContributeTo score": 1.0, "reason": "SYNTHETIC"}]
        else:
            value = ["Complete the operational task."]
        output = json.dumps(value)
        self.cache.put(key, output, {"mode": "mock", "usage": None})
        return output


def make_defense(group, *, config, mode, cache, events, scope, event_path, base_url, api_key):
    method = METHODS[group]
    if mode == "mock":
        backend = SyntheticDefense(method, cache)
    else:
        backend = CachedChatModel(
            role="external_" + method, model=config["guard_model"], base_url=base_url,
            api_key=api_key, cache=cache, max_retries=0, temperature=0.0,
            timeout=config["timeout"], cache_namespace=VERSION + ":" + method,
            max_output_tokens=config["guard_max_output_tokens"] or None,
            output_token_field=config["guard_output_token_field"],
            thinking_mode=config["guard_thinking_mode"],
            reasoning_effort=config.get("guard_reasoning_effort"))
    tracked = TrackedModel(backend, "external_" + method, events, scope, event_path,
                           mode=mode, model=config["guard_model"])
    factory = MemoryUseTaskShield if method == "task_shield" else DEFENSE_REGISTRY[method]
    return factory(model=tracked, model_pairing="follow_target" if config.get("model") == config["guard_model"] else "independent")
