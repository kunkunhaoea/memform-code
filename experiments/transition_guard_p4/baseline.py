"""Read-only reuse of a compatible B0; never call a model to create a baseline."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from memform.common import read_jsonl, stable_hash
from .analyze_results import valid_outputs


class PairedGuard:
    """Reject a changed B0 candidate before spending any guard API calls."""
    def __init__(self, guard, memories):
        self.guard = guard
        self.expected = iter([memories["clean"], memories["poisoned"]])

    def review(self, transition, updater):
        if transition.proposed_memory != next(self.expected):
            raise ValueError("Candidate memory differs from the paired B0 before guard execution")
        return self.guard.review(transition, updater)


def load_baseline(directory: Path, configuration: dict[str, Any], active: list[dict[str, Any]]):
    previous = json.loads((directory / "run_config.json").read_text())
    identity = ("model", "temperature", "max_memory_words", "source_sha256", "endpoint_fingerprint")
    mismatches = [k for k in identity if previous.get(k) != configuration.get(k)]
    # B0 depends on Agent + Updater, not on any defense-only setting. Older
    # configurations had one shared switch (or used the provider default).
    for role in ("agent", "memory"):
        key = role + "_thinking_mode"
        if previous.get(key, previous.get("thinking_mode", "default")) != configuration.get(key, configuration.get("thinking_mode", "default")):
            mismatches.append(key)
    if configuration["mode"] != "validate" and previous.get("mode") != configuration["mode"]:
        mismatches.append("mode")
    if Path(previous["cache"]).resolve() != Path(configuration["cache"]).resolve():
        mismatches.append("cache")
    if mismatches:
        raise ValueError("Baseline configuration mismatch: " + ", ".join(mismatches))
    source = directory / "case_results.jsonl"
    candidates = [r for r in read_jsonl(source) if r.get("experiment_group") == "B0"] if source.exists() else []
    if not candidates and (directory / "baseline_results.jsonl").exists():
        candidates = read_jsonl(directory / "baseline_results.jsonl")
    if len({r["case_id"] for r in candidates}) != len(candidates):
        raise ValueError("Duplicate external baseline rows")
    index = {r["case_id"]: r for r in candidates}
    retained = []
    for case in active:
        row = index.get(case["case_id"])
        if row is None or row.get("experiment_group") != "B0" or not valid_outputs(row):
            raise ValueError("Missing or invalid external B0 row: " + case["case_id"])
        expected = case["evaluation"]["trigger_tasks"]
        tasks = row["task_results"]
        if ([t["task_id"] for t in tasks] != [t["task_id"] for t in expected]
                or any(t[label] != e[label] for t, e in zip(tasks, expected)
                       for label in ("clean_answer", "poisoned_answer"))):
            raise ValueError("External baseline task identity mismatch")
        retained.append(row)
    fingerprint = stable_hash({"identity": {k: previous.get(k) for k in identity},
                               "target_thinking_modes": {
                                   role: previous.get(role + "_thinking_mode", previous.get("thinking_mode", "default"))
                                   for role in ("agent", "memory")}, "rows": retained})
    return retained, fingerprint
