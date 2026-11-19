"""Experiment-only call telemetry and an explicitly synthetic offline backend."""
from __future__ import annotations

import json
import re
import time
from contextlib import contextmanager
from typing import Any

from memform.common import stable_hash
from memform.eval.core import CachedChatModel, DiskCache
from memform.defenses.transition_guard.guard import VERSION


class SimulatedModel:
    """Protocol smoke test only; never estimates model/defense effectiveness."""
    def __init__(self, role: str, cache: DiskCache):
        self.role, self.cache = role, cache
        self.cache_hits = self.real_calls = self.simulated_calls = 0
        self.last_call_usage = None

    def invoke(self, system: str, user: str) -> str:
        key = stable_hash({"mode": "SYNTHETIC_MOCK_V1", "role": self.role,
                           "system": system, "user": user})
        cached = self.cache.get(key)
        if cached is not None:
            self.cache_hits += 1
            return cached
        self.simulated_calls += 1
        if self.role == "agent":
            labels = re.search(r"one of ([^>]+)>", system)
            label = labels.group(1).split(",")[0].strip() if labels else "A"
            output = f"SYNTHETIC MOCK, no reasoning performed.\nFINAL_ANSWER: {label}"
        elif self.role == "memory":
            output = "SYNTHETIC MOCK POLICY " + stable_hash(user)[:16]
        else:
            payload = json.loads(user)
            if self.role == "selection":
                omitted = payload.get("omitted", [])[:payload["limit"]]
                output = json.dumps({"witnesses": [dict(record_id=e["record_id"], quote=e["text"])
                                                     for e in omitted]})
            else:
                output = json.dumps({"verdict": "inconclusive", "material_change": False,
                                     "reason": "Synthetic backend performs no semantic verification"})
        self.cache.put(key, output, {"mode": "mock", "usage": None})
        return output


class TrackedModel:
    def __init__(self, backend: Any, role: str, events: list[dict[str, Any]],
                 scope: dict[str, Any], event_path: Any, *, mode: str, model: str):
        self.backend, self.role, self.events = backend, role, events
        self.scope, self.event_path, self.mode, self.model = scope, event_path, mode, model
        self.purpose = role

    @contextmanager
    def stage(self, purpose: str):
        previous = self.purpose
        self.purpose = purpose
        try:
            yield
        finally:
            self.purpose = previous

    @property
    def last_call_metadata(self):
        return getattr(self.backend, "last_call_metadata", {})

    def invoke(self, system: str, user: str) -> str:
        started = time.perf_counter()
        hits = self.backend.cache_hits
        output, error = None, None
        error_chain: list[str] = []
        try:
            output = self.backend.invoke(system, user)
            return output
        except Exception as exc:
            error = type(exc).__name__
            cause: BaseException | None = exc
            seen: set[int] = set()
            while cause is not None and id(cause) not in seen and len(error_chain) < 8:
                seen.add(id(cause))
                error_chain.append(type(cause).__name__)
                cause = cause.__cause__ or cause.__context__
            raise
        finally:
            cached = self.backend.cache_hits > hits
            event = {**self.scope, "event_id": len(self.events), "role": self.role,
                     "stage": self.purpose, "mode": self.mode, "model": self.model,
                     "cache_hit": cached, "success": error is None, "error_type": error,
                     "error_chain": error_chain,
                     # max_retries=0: an uncached live invocation has at most one HTTP attempt.
                     "api_attempts": int(self.mode == "live" and not cached),
                     "simulated_generation": bool(self.mode == "mock" and not cached),
                     "usage": None if cached else self.backend.last_call_usage,
                     "cached_response_usage": self.backend.last_call_usage if cached else None,
                     "response_metadata": self.last_call_metadata,
                     "latency_seconds": time.perf_counter() - started,
                     "system": system, "user": user, "raw_output": output}
            self.events.append(event)
            with self.event_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")


def make_models(*, mode: str, model: str, guard_model: str, base_url: str | None,
                api_key: str | None, cache: DiskCache, events: list[dict[str, Any]],
                scope: dict[str, Any], event_path: Any, timeout: float,
                temperature: float, guard_max_output_tokens: int | None = None,
                guard_output_token_field: str = "auto",
                guard_reasoning_effort: str | None = None,
                thinking_mode: str = "default",
                agent_thinking_mode: str | None = None,
                memory_thinking_mode: str | None = None,
                guard_thinking_mode: str | None = None) -> dict[str, TrackedModel]:
    # Keep the shared switch for existing callers. The experiment CLI supplies
    # explicit per-role modes, including the same memory backend for CF replay.
    thinking_modes = {"agent": agent_thinking_mode or thinking_mode,
                      "memory": memory_thinking_mode or thinking_mode,
                      "guard": guard_thinking_mode or thinking_mode}
    if any(value not in {"default", "on", "off"} for value in thinking_modes.values()):
        raise ValueError("Unknown thinking mode")
    if thinking_modes["guard"] == "off" and guard_reasoning_effort is not None:
        raise ValueError("Disabled guard thinking cannot be combined with a reasoning effort")
    models = {}
    for role in ("agent", "memory", "selection", "verification", "repair", "repair_check", "final_judge"):
        name = model if role in {"agent", "memory"} else guard_model
        if mode == "mock":
            backend = SimulatedModel(role, cache)
        else:
            backend = CachedChatModel(
                role=role, model=name, base_url=base_url, api_key=api_key, cache=cache,
                max_retries=0, temperature=temperature, timeout=timeout,
                cache_namespace="" if role in {"agent", "memory"} else VERSION + ":" + role,
                max_output_tokens=None if role in {"agent", "memory"} else guard_max_output_tokens,
                output_token_field=guard_output_token_field,
                reasoning_effort=None if role in {"agent", "memory"} else guard_reasoning_effort,
                thinking_mode=thinking_modes[role if role in {"agent", "memory"} else "guard"],
            )
        models[role] = TrackedModel(backend, role, events, scope, event_path, mode=mode, model=name)
    return models


def summarize_cost(events: list[dict[str, Any]]) -> dict[str, Any]:
    actual = [e for e in events if e["api_attempts"]]
    missing = sum(e.get("usage") is None for e in actual)
    simulated = any(e["mode"] == "mock" for e in events)
    known = [e["usage"] for e in actual if e.get("usage") is not None]
    return {"logical_invocations": len(events), "api_attempts": len(actual),
            "successful_api_responses": sum(e["success"] for e in actual),
            "cache_hits": sum(e["cache_hit"] for e in events),
            "simulated_generations": sum(e["simulated_generation"] for e in events),
            "input_tokens": None if missing or simulated else sum(u["input_tokens"] for u in known),
            "output_tokens": None if missing or simulated else sum(u["output_tokens"] for u in known),
            "api_responses_without_usage": missing,
            "token_usage_status": "not_applicable_mock" if simulated else "unavailable_or_partial" if missing else "available",
            "known_input_tokens": sum(u["input_tokens"] for u in known),
            "known_output_tokens": sum(u["output_tokens"] for u in known),
            "latency_seconds": sum(e["latency_seconds"] for e in events),
            "output_limit_exceeded_responses": sum(bool(e.get("response_metadata", {}).get("output_limit_exceeded")) for e in actual),
            "length_limited_responses": sum(e.get("response_metadata", {}).get("finish_reason") == "length" for e in actual),
            "known_reasoning_tokens": sum(e.get("response_metadata", {}).get("reasoning_tokens") or 0 for e in actual),
            "responses_without_reasoning_breakdown": sum(e.get("response_metadata", {}).get("reasoning_tokens") is None for e in actual),
            "thinking_mode_violations": sum(e.get("response_metadata", {}).get("thinking_mode_violation") is True for e in actual),
            "counterfactual_replay_invocations": sum(e["stage"] == "counterfactual_replay" for e in events),
            "counterfactual_replay_api_attempts": sum(e["stage"] == "counterfactual_replay" and e["api_attempts"] for e in events)}
