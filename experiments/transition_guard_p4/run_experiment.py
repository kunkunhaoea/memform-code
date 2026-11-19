#!/usr/bin/env python3
"""Run B0-B5 through MemForm's existing LangChain case runner and scorer."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memform.common import load_env, read_jsonl, stable_hash, validate_compiled_case, write_json
from memform.defenses.metrics import file_sha256
from memform.defenses.common import EMPTY_MEMORY
from memform.defenses.transition_guard.guard import GROUPS, VERSION, TransitionGuard
from memform.defenses.transition_guard.audit import AUDIT_SCHEMA
from memform.defenses.transition_guard.interface import GuardResult
from memform.eval.core import DiskCache, run_case
from experiments.transition_guard_p4.runtime import make_models, summarize_cost
from experiments.transition_guard_p4.analyze_results import analyze_directory, valid_outputs
from experiments.transition_guard_p4.baseline import load_baseline, PairedGuard


def select_cases(cases: list[dict[str, Any]], count: int, seed: int) -> list[dict[str, Any]]:
    """One item per seeded family first; never inspect target-model outcomes."""
    rng = random.Random(seed)
    families: dict[str, list[dict[str, Any]]] = {}
    for case in sorted(cases, key=lambda c: c["case_id"]):
        families.setdefault(case["family_id"], []).append(case)
    names = sorted(families)
    rng.shuffle(names)
    count = len(cases) if count == 0 else count
    if not 1 <= count <= len(cases):
        raise ValueError(f"Sample size must be 1..{len(cases)}, or 0 for all P4")
    chosen = [rng.choice(families[name]) for name in names[:count]]
    selected = {c["case_id"] for c in chosen}
    remainder = sorted((c for c in cases if c["case_id"] not in selected), key=lambda c: c["case_id"])
    rng.shuffle(remainder)
    return chosen + remainder[:count - len(chosen)]


def append_row(path: Path, value: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")


def load_resume(
    directory: Path, configuration: dict[str, Any], manifest: dict[str, Any],
    active: list[dict[str, Any]],
) -> tuple[dict[tuple[str, str], dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Retain completed cells without rerunning them; never select by correctness."""
    previous = json.loads((directory / "run_config.json").read_text())
    old_manifest = json.loads((directory / "selected_cases.json").read_text())
    identity_fields = (
        "version", "audit_schema", "seed", "groups", "strategy", "k", "max_rebuilds", "model", "guard_model",
        "temperature", "max_memory_words", "source_sha256", "active_case_ids", "endpoint_fingerprint",
        "defer_semantics",
        "always_defer_control_version",
        "implementation_sha256", "selection_max_records", "selection_max_chars",
        "guard_max_input_chars", "guard_max_output_tokens",
        "guard_output_token_field", "repair_length_retries", "baseline_fingerprint",
        "guard_reasoning_effort",
        "thinking_mode", "agent_thinking_mode", "memory_thinking_mode", "guard_thinking_mode",
    )
    mismatches = [key for key in identity_fields if previous.get(key) != configuration.get(key)]
    if configuration["mode"] != "validate" and previous.get("mode") != configuration["mode"]:
        mismatches.append("mode")
    if Path(previous["cache"]).resolve() != Path(configuration["cache"]).resolve():
        mismatches.append("cache")
    if old_manifest.get("case_ids") != manifest.get("case_ids"):
        mismatches.append("manifest.case_ids")
    if mismatches:
        raise ValueError("Resume configuration mismatch: " + ", ".join(mismatches))
    source_events = directory / "call_events.jsonl"
    events = read_jsonl(source_events) if source_events.exists() else []
    if any(e.get("event_id") != i for i, e in enumerate(events)):
        raise ValueError("Source event IDs must be contiguous for preserved result references")
    for event in events:
        event.setdefault("origin_run", str(directory.resolve()))
    cases = {c["case_id"]: c for c in active}
    retained = {}
    seen = set()
    source_rows = directory / "case_results.jsonl"
    for row in read_jsonl(source_rows) if source_rows.exists() else []:
        key = (row.get("experiment_group"), row.get("case_id"))
        if key in seen:
            raise ValueError(f"Duplicate result cell in resume source: {key}")
        seen.add(key)
        if key[0] not in configuration["groups"] or key[1] not in cases:
            raise ValueError(f"Unexpected resume result cell: {key}")
        expected_tasks = cases[key[1]]["evaluation"]["trigger_tasks"]
        tasks = row.get("task_results") or []
        if [t.get("task_id") for t in tasks] != [t["task_id"] for t in expected_tasks]:
            raise ValueError(f"Resume task identity mismatch: {key}")
        for task, expected in zip(tasks, expected_tasks):
            if any(task.get(label) != expected[label] for label in ("clean_answer", "poisoned_answer")):
                raise ValueError(f"Resume scoring labels differ from frozen case: {key}")
        span = row.get("call_event_range")
        if not isinstance(span, list) or len(span) != 2 or not (0 <= span[0] <= span[1] <= len(events)):
            raise ValueError(f"Invalid resume call-event range: {key}")
        if valid_outputs(row):
            retained[key] = row
    historical = []
    for filename in ("prior_errors.jsonl", "errors.jsonl"):
        path = directory / filename
        if path.exists():
            for error in read_jsonl(path):
                error.setdefault("origin_run", str(directory.resolve()))
                historical.append(error)
    pending = [(group, c["case_id"]) for group in configuration["groups"] for c in active
               if (group, c["case_id"]) not in retained]
    plan = {"source_directory": str(directory.resolve()), "retained_cells": len(retained),
            "pending_cells": len(pending), "pending": [{"group": g, "case_id": cid} for g, cid in pending],
            "retention_rule": "completed valid predictions, regardless of correctness or defense outcome",
            "source_results_sha256": file_sha256(source_rows) if source_rows.exists() else None,
            "inherited_call_events": len(events), "historical_failures": len(historical)}
    return retained, events, historical, plan


def main() -> None:
    load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["live", "mock", "validate"], default="live")
    parser.add_argument("--cases", type=Path, default=ROOT / "outputs/mcq_item_audit/accepted_cases.jsonl")
    parser.add_argument("--sample-size", type=int, default=16, help="0 selects the complete P4 MCQ dataset")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--manifest", type=Path, help="Reuse an exact selected_cases.json from an earlier run")
    parser.add_argument("--resume-from", type=Path,
                        help="Reuse completed case/group rows from a prior run; execute only missing/invalid cells")
    parser.add_argument("--baseline-from", type=Path,
                        help="Reuse existing B0 from the SAME endpoint/model/cache; no baseline API calls")
    parser.add_argument("--limit", type=int, default=0, help="Smoke test a prefix without changing the full manifest")
    parser.add_argument("--groups", nargs="+", choices=list(GROUPS), default=["B4"],
                        help="Default B4 only; explicitly request other groups when needed")
    parser.add_argument("--strategy", choices=["swap", "augment"], default="swap")
    parser.add_argument("--k", type=int, default=1)
    parser.add_argument("--max-rebuilds", type=int, default=2)
    parser.add_argument("--selection-max-records", type=int, default=8,
                        help="Maximum omitted records locally shortlisted for the guided selector")
    parser.add_argument("--selection-max-chars", type=int, default=6000,
                        help="Maximum serialized shortlist characters; observations are never truncated")
    parser.add_argument("--guard-max-input-chars", type=int, default=24000,
                        help="Maximum system+user characters per guard request and bounded CF replay")
    parser.add_argument("--guard-max-output-tokens", type=int, default=0,
                        help="Guard-only API output cap (0: provider default); may include reasoning tokens")
    parser.add_argument("--guard-output-token-field", choices=["auto", "max_tokens", "max_completion_tokens"], default="auto")
    parser.add_argument("--guard-reasoning-effort", choices=["low", "high", "max"],
                        help="Optional guard-only reasoning effort; omission keeps the provider default")
    parser.add_argument("--thinking", choices=["off", "on", "default"],
                        help="Legacy shared mode; per-role flags override it. Without flags: agent/memory on, guard off")
    parser.add_argument("--agent-thinking", choices=["off", "on", "default"],
                        help="Task Agent thinking (default: on unless --thinking is supplied)")
    parser.add_argument("--memory-thinking", choices=["off", "on", "default"],
                        help="Original AND counterfactual Updater thinking (default: on unless --thinking is supplied)")
    parser.add_argument("--guard-thinking", choices=["off", "on", "default"],
                        help="Selection/verification/repair/check/final-judge thinking (default: off)")
    parser.add_argument("--repair-length-retries", type=int, choices=[0, 1], default=1,
                        help="At most one extra repair call, only for a structurally valid overlong draft")
    parser.add_argument("--model", default=os.environ.get("MEMFORM_MODEL", "gpt-4.1-nano"))
    parser.add_argument("--guard-model", default=None, help="Defaults to the target model; same across B1/B3/B4")
    parser.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL"))
    parser.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY"))
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--max-memory-words", type=int, default=120)
    parser.add_argument("--cache", type=Path, default=ROOT / ".cache/transition_guard_p4.jsonl")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    for role, default in (("agent", "on"), ("memory", "on"), ("guard", "off")):
        field = role + "_thinking"
        setattr(args, field, getattr(args, field) or args.thinking or default)
    if args.guard_thinking == "off" and args.guard_reasoning_effort is not None:
        parser.error("Guard thinking is off; --guard-reasoning-effort requires --guard-thinking on/default")
    if args.k < 1 or args.max_rebuilds < 1 or args.k * args.max_rebuilds > 2:
        parser.error("MVP witness budget: k * max-rebuilds must be 1 or 2")
    if args.limit < 0 or args.sample_size < 0 or args.max_memory_words < 1 or args.timeout <= 0:
        parser.error("Invalid count or timeout")
    if min(args.selection_max_records, args.selection_max_chars, args.guard_max_input_chars) < 1 or args.guard_max_output_tokens < 0:
        parser.error("Invalid guard context/output budget")
    if len(set(args.groups)) != len(args.groups):
        parser.error("Groups must be unique")
    if args.resume_from and args.baseline_from is None:
        previous = json.loads((args.resume_from / "run_config.json").read_text())
        if previous.get("baseline_from"):
            args.baseline_from = Path(previous["baseline_from"])
    if args.baseline_from and "B0" in args.groups:
        parser.error("Use --baseline-from or execute B0, not both")
    if args.resume_from and args.manifest is None:
        args.manifest = args.resume_from / "selected_cases.json"
    source_hash = file_sha256(args.cases)
    cases = [c for c in read_jsonl(args.cases)
             if c.get("interface") == "P4" and c.get("evaluation", {}).get("type") == "multiple_choice"]
    for case in cases:
        errors = validate_compiled_case(case)
        if errors:
            raise ValueError(f"Invalid frozen case {case['case_id']}: {errors}")
    if args.manifest:
        manifest = json.loads(args.manifest.read_text())
        if manifest["source_sha256"] != source_hash:
            raise ValueError("Manifest source fingerprint mismatch")
        indexed = {c["case_id"]: c for c in cases}
        selected = [indexed[cid] for cid in manifest["case_ids"]]
        if len({c['case_id'] for c in selected}) != len(selected):
            raise ValueError("Duplicate case IDs in manifest")
        if args.seed != manifest["seed"]:
            parser.error("--seed must match the supplied manifest")
    else:
        selected = select_cases(cases, args.sample_size, args.seed)
        manifest = {"seed": args.seed, "source": str(args.cases.resolve()),
                    "source_sha256": source_hash, "selection_rule": "seeded_one_per_family_first",
                    "case_ids": [c["case_id"] for c in selected],
                    "family_ids": [c["family_id"] for c in selected]}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    out = args.output_dir or ROOT / "results/transition_guard_p4" / f"{stamp}_{args.mode}"
    active = selected[:args.limit] if args.limit else selected
    groups = [g for g in GROUPS if g in args.groups]
    configuration = {"version": VERSION, "audit_schema": AUDIT_SCHEMA, "mode": args.mode,
                     "implementation_sha256": stable_hash({
                         name: file_sha256(ROOT / name) for name in (
                             "memform/defenses/transition_guard/guard.py",
                             "memform/defenses/transition_guard/audit.py",
                             "memform/defenses/transition_guard/citations.py",
                             "memform/defenses/transition_guard/retrieval.py",
                             "memform/defenses/transition_guard/interface.py",
                             "memform/defenses/transition_guard/adapter.py",
                             "memform/eval/core.py", "memform/defenses/metrics.py",
                             "memform/llm_compat.py", "experiments/transition_guard_p4/baseline.py",
                             "experiments/transition_guard_p4/runtime.py",
                             "experiments/transition_guard_p4/run_experiment.py",
                             "experiments/transition_guard_p4/analyze_results.py")}),
                     "synthetic_results": args.mode == "mock", "seed": args.seed,
                     "groups": groups, "strategy": args.strategy, "k": args.k,
                     "max_rebuilds": args.max_rebuilds, "model": args.model,
                     "selection_max_records": args.selection_max_records,
                     "selection_max_chars": args.selection_max_chars,
                     "guard_max_input_chars": args.guard_max_input_chars,
                     "guard_max_output_tokens": args.guard_max_output_tokens,
                     "guard_output_token_field": args.guard_output_token_field,
                     "guard_reasoning_effort": args.guard_reasoning_effort,
                     "thinking_mode": args.agent_thinking if args.agent_thinking == args.memory_thinking == args.guard_thinking else "mixed",
                     "agent_thinking_mode": args.agent_thinking,
                     "memory_thinking_mode": args.memory_thinking,
                     "guard_thinking_mode": args.guard_thinking,
                     "repair_length_retries": args.repair_length_retries,
                     "guard_model": args.guard_model or args.model,
                     "temperature": args.temperature, "max_memory_words": args.max_memory_words,
                     "source_sha256": source_hash, "selected_cases": len(selected),
                     "active_cases": len(active), "smoke_prefix": bool(args.limit),
                     "active_case_ids": [c["case_id"] for c in active],
                     "endpoint_fingerprint": stable_hash(args.base_url or "default"),
                     "cache": str(args.cache), "http_retries": 0,
                     "timeout": args.timeout,
                     "defer_semantics": "previous_memory_or_empty_policy_then_original_agent",
                     "always_defer_control_version": "always-defer-v1" if "B5" in groups else None,
                     "known_issues_out_of_scope": ["P3 count_matched_clean is not built", "CLI construction commands absent"],
                     "paper": "https://arxiv.org/html/2610.14431"}
    baseline_rows = []
    configuration["baseline_fingerprint"] = None
    if args.baseline_from:
        baseline_rows, fingerprint = load_baseline(args.baseline_from, configuration, active)
        configuration.update(baseline_from=str(args.baseline_from.resolve()), baseline_fingerprint=fingerprint)
    retained: dict[tuple[str, str], dict[str, Any]] = {}
    events: list[dict[str, Any]] = []
    historical: list[dict[str, Any]] = []
    resume_plan = None
    if args.resume_from:
        retained, events, historical, resume_plan = load_resume(args.resume_from, configuration, manifest, active)
        configuration["resume_from"] = str(args.resume_from.resolve())
        configuration["inherited_call_events"] = len(events)
        configuration["retained_cells"] = len(retained)
    pending_count = len(groups) * len(active) - len(retained)
    # Refuse overwrites, including the source directory and interrupted runs.
    out.mkdir(parents=True, exist_ok=False)
    write_json(out / "selected_cases.json", manifest)
    write_json(out / "run_config.json", configuration)
    for row in baseline_rows:
        append_row(out / "baseline_results.jsonl", row)
    if resume_plan is not None:
        write_json(out / "resume_plan.json", resume_plan)
        print(f"[resume] retained={len(retained)} pending={pending_count}", flush=True)
    if args.mode == "validate":
        write_json(out / "summary.json", {"status": "validated_only", "configuration": configuration,
                                          "eligible": None, "model_performance": None})
        print(json.dumps({"status": "validated_only", "output_dir": str(out),
                          "cases": len(active), "groups": groups, "retained_cells": len(retained),
                          "pending_cells": pending_count}, ensure_ascii=False))
        return
    if pending_count and args.mode == "live" and not args.api_key:
        write_json(out / "online_status.json", {"status": "not_run", "reason": "OPENAI_API_KEY is not configured"})
        raise SystemExit(f"No API credential; live experiments were not run. Manifest saved in {out}")
    if pending_count and args.mode == "live" and importlib.util.find_spec("langchain_openai") is None:
        write_json(out / "online_status.json", {"status": "not_run", "reason": "langchain_openai is not installed"})
        raise SystemExit("Install project dependencies before --mode live")
    print(f"[thinking] agent={args.agent_thinking} memory={args.memory_thinking} "
          f"counterfactual={args.memory_thinking} guard={args.guard_thinking}", flush=True)
    for event in events:
        append_row(out / "call_events.jsonl", event)
    for error in historical:
        append_row(out / "prior_errors.jsonl", error)
    scope: dict[str, Any] = {"origin_run": str(out.resolve())}
    models = {}
    if pending_count:
        cache = DiskCache(args.cache)
        models = make_models(mode=args.mode, model=args.model, guard_model=args.guard_model or args.model,
                             base_url=args.base_url, api_key=args.api_key, cache=cache,
                             events=events, scope=scope, event_path=out / "call_events.jsonl",
                             timeout=args.timeout, temperature=args.temperature,
                             guard_max_output_tokens=args.guard_max_output_tokens or None,
                             guard_output_token_field=args.guard_output_token_field,
                             guard_reasoning_effort=args.guard_reasoning_effort,
                             agent_thinking_mode=args.agent_thinking,
                             memory_thinking_mode=args.memory_thinking,
                             guard_thinking_mode=args.guard_thinking)
    originals: dict[str, dict[str, Any]] = {r["case_id"]: r for r in baseline_rows}
    for group in groups:
        guard = None if group in {"B0", "B5"} else TransitionGuard(
            group, models, strategy=args.strategy, k=args.k,
            max_rebuilds=args.max_rebuilds, seed=args.seed,
            selection_max_records=args.selection_max_records,
            selection_max_chars=args.selection_max_chars,
            guard_max_input_chars=args.guard_max_input_chars,
            repair_length_retries=args.repair_length_retries,
        )
        for case in active:
            key = (group, case["case_id"])
            if key in retained:
                row = dict(retained[key])
                row.setdefault("result_origin", str(args.resume_from.resolve()))
                row["resumed_from"] = str(args.resume_from.resolve())
                if group == "B0":
                    originals[case["case_id"]] = row
                else:
                    baseline = originals.get(case["case_id"])
                    if baseline is not None:
                        for side in ("clean", "poisoned"):
                            if row["transition_reviews"][side]["original_memory"] != baseline["learned_memories"][side]:
                                raise ValueError("Retained candidate does not match the fixed B0 candidate")
                        row["original_task_results"] = baseline["task_results"]
                        row["baseline_available"] = True
                append_row(out / "case_results.jsonl", row)
                print(f"[{args.mode}] {group} {case['case_id']} reused", flush=True)
                continue
            scope.update(group=group, case_id=case["case_id"], family_id=case["family_id"])
            before = len(events)
            started = time.perf_counter()
            try:
                paired = originals.get(case["case_id"])
                case_guard = PairedGuard(guard, paired["learned_memories"]) if guard is not None and paired else guard
                row = run_case(models["agent"], models["memory"], None, None, None,
                               case, "learned", args.max_memory_words, "not_applicable", False,
                               post_consolidation_guard=case_guard, run_mechanism_controls=False,
                               always_defer=group == "B5")
                original = row if group == "B0" else originals.get(case["case_id"])
                row.update(experiment_group=group, group_name=GROUPS[group], model=args.model,
                           mode=args.mode, synthetic_results=args.mode == "mock",
                           original_task_results=original["task_results"] if original else None,
                           baseline_available=original is not None,
                           execution_status="ok", elapsed_seconds=time.perf_counter() - started)
                reviews = row["memory_traces"].get("post_consolidation")
                if group == "B5":
                    reviews = {side: GuardResult(
                        original["learned_memories"][side] if original else "", EMPTY_MEMORY,
                        commit_decision="defer", status="always_defer",
                        candidate_pool_size=len(case["candidate_experiences"]),
                        selected_count=len(case["memory_building"][f"{side}_condition"]["order"]),
                        omitted_count=len(case["candidate_experiences"]) - len(case["memory_building"][f"{side}_condition"]["order"]),
                    ).to_dict() for side in ("clean", "poisoned")}
                    row["memory_traces"]["post_consolidation"] = reviews
                    row["candidate_memory_source"] = "paired_B0" if original else "not_generated"
                elif reviews is None:
                    reviews = {side: GuardResult(
                        row["learned_memories"][side], row["learned_memories"][side], status="disabled",
                        candidate_pool_size=len(case["candidate_experiences"]),
                        selected_count=len(case["memory_building"][f"{side}_condition"]["order"]),
                        omitted_count=len(case["candidate_experiences"]) - len(case["memory_building"][f"{side}_condition"]["order"]),
                    ).to_dict() for side in ("clean", "poisoned")}
                row["transition_reviews"] = reviews
                if original is not None:
                    for side in ("clean", "poisoned"):
                        if reviews[side]["original_memory"] != original["learned_memories"][side]:
                            raise RuntimeError("Candidate memory differs from the paired B0 candidate")
                if group == "B0":
                    originals[case["case_id"]] = row
                row["cost"] = summarize_cost(events[before:])
                row["call_event_range"] = [before, len(events)]
                append_row(out / "case_results.jsonl", row)
                print(f"[{args.mode}] {group} {case['case_id']} ok", flush=True)
            except Exception as exc:
                # Do not log credentials, endpoint bodies or arbitrary exception reprs.
                append_row(out / "errors.jsonl", {**scope, "error_type": type(exc).__name__,
                                                  "error_chain": events[-1].get("error_chain", []) if len(events) > before else [type(exc).__name__],
                                                  "elapsed_seconds": time.perf_counter() - started,
                                                  "cost": summarize_cost(events[before:])})
                print(f"[{args.mode}] {group} {case['case_id']} failed: {type(exc).__name__}", flush=True)
    analyze_directory(out)
    print(f"Results: {out}")


if __name__ == "__main__":
    main()
