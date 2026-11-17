"""Offline tests of protocol invariants, not empirical defense effectiveness."""
from __future__ import annotations

import copy
import importlib
import json
import re
import subprocess
import sys
import tempfile
import types
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from memform.common import read_jsonl, stable_hash, validate_compiled_case, write_jsonl
from memform.defenses.common import EMPTY_MEMORY
from memform.defenses.metrics import build_eligibility, index_case_rows, score_on_eligibility
from memform.defenses.transition_guard import Evidence, MemoryTransition, UpdateContext, TransitionGuard
from memform.defenses.transition_guard.audit import AUDIT_SCHEMA, CHECKS
from memform.defenses.transition_guard.adapter import p4_transition
from memform.eval.core import CachedChatModel, DiskCache, build_learned_memories, run_case
from memform.freeform.evaluator import validate_freeform_case_for_evaluation
from experiments.transition_guard_p4.analyze_results import analyze_directory
from experiments.transition_guard_p4.run_experiment import ROOT, select_cases
from experiments.transition_guard_p4.runtime import summarize_cost, TrackedModel


def transition() -> MemoryTransition:
    items = (
        Evidence("record_001", "Matching authenticated account and order: merging duplicate tickets succeeded."),
        Evidence("record_002", "Another matching authenticated account and order: merging duplicate tickets succeeded."),
        Evidence("record_003", "Different authenticated accounts: keeping tickets separate prevented exposing another account."),
        Evidence("record_004", "Missing order verification: keeping tickets separate prevented merging unrelated requests."),
    )
    return MemoryTransition(None, items, items[:2], "Merge all duplicate tickets.",
                            UpdateContext("How to route duplicate tickets", "Consolidate observations.", 120))


class Reviewer:
    def __init__(self, stage: str, mode: str = "gap"):
        self.stage, self.mode, self.prompts = stage, mode, []

    def invoke(self, system: str, user: str) -> str:
        p = json.loads(user)
        self.prompts.append((system, p))
        if self.mode == "malformed":
            return "not JSON"
        evidence = p.get("evidence") or p.get("selected", []) + p.get("witnesses", [])
        citations = [{"record_id": e["record_id"], "quote": e["text"]} for e in evidence]
        if self.mode == "bad_quotes":
            citations = [{"record_id": "record_003", "quote": "This observation never existed anywhere."}]
        if self.stage == "selection":
            return json.dumps({"witnesses": [{"record_id": e["record_id"], "quote": e["text"], "reason": "Scope exception"}
                                              for e in p["omitted"][:p["limit"]]]})
        if self.stage in {"verification", "final_judge"}:
            return json.dumps({"verdict": "evidence_gap" if self.mode not in {"no_gap", "inconclusive"} else self.mode,
                               "change_type": "scope_narrowing", "material_change": True,
                               "defective_claim": p.get("original_memory", p.get("proposed_memory")),
                               "witness_valid": True, "original_support_defect": True,
                               "counterfactual_better_supported": True,
                               "alternative_explanation": "omission_supported" if self.mode != "stochastic" else "stochastic_or_underdetermined",
                               "citations": citations, "reason": "An omitted record exposes missing account verification."})
        if self.stage == "repair":
            return json.dumps({"status": "defer" if self.mode == "abstain" else "repaired",
                               "edits": [{"old": p["original_memory"],
                                          "new": "Merge only when authenticated account and order match; otherwise keep separate.",
                                          "effect": "restrict", "citations": citations}],
                               "reason": "Preserve matching cases and constrain the scope."})
        checks = [{"check": name, "status": "pass", "findings": []} for name in CHECKS]
        if self.mode == "reject_repair":
            checks[1].update(status="fail", findings=[{
                "original_quotes": [], "policy_quotes": [p["repaired_memory"]],
                "citations": citations, "reason": "Synthetic rejection with located claim."}])
        return json.dumps({"schema_version": AUDIT_SCHEMA, "checks": checks,
                           "selected_evidence_review": {
                               "status": "qualified", "citations": citations[:1],
                               "policy_quotes": [p["repaired_memory"]],
                               "reason": "Synthetic source-preservation review."},
                           "policy_units": [{
                               "policy_quote": p["repaired_memory"], "kind": "permission",
                               "status": "supported", "introduces_permission": False,
                               "evidence_checks": [{**c, "relation": "supports",
                                                    "reason": "Synthetic branch support."} for c in citations[:2]],
                               "reason": "Synthetic policy coverage."}],
                           "evidence_conflicts": [], "uncertainty_quotes": [],
                           "edit_checks": [{"index": i, "status": "supported", "effect": "restrict", "introduces_permission": False, "citations": citations}
                                           for i, _ in enumerate(p["edits"])],
                           "witness_checks": [{"record_id": rid, "status": "consistent",
                                               "citations": [c for c in citations if c["record_id"] == rid],
                                               "policy_quotes": [p["repaired_memory"]]}
                                              for rid in p["required_witness_ids"]],
                           "reason": "Evidence supports this scope."})


def reviewers(mode: str = "gap") -> dict[str, Reviewer]:
    return {s: Reviewer(s, mode) for s in ("selection", "verification", "final_judge", "repair", "repair_check")}


class ProtocolLLM:
    def __init__(self):
        self.calls = []

    def invoke(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return "FINAL_ANSWER: A" if "FINAL_ANSWER" in system else "policy " + stable_hash(user)[:12]


class TransitionTests(unittest.TestCase):
    def test_omissions_and_invariants(self):
        t = transition()
        self.assertEqual([e.record_id for e in t.omitted_experiences], ["record_003", "record_004"])
        with self.assertRaises(ValueError):
            MemoryTransition(None, t.candidate_experiences, (Evidence("unknown", "x"),), "rule", t.update_context)

    def test_k_one_replay_and_repair(self):
        calls = []
        def updater(items):
            calls.append(items)
            return "Only merge matching accounts and orders."
        result = TransitionGuard("B4", reviewers(), k=1, max_rebuilds=2).review(transition(), updater)
        self.assertEqual(result.commit_decision, "commit_repaired")
        self.assertEqual(result.status, "repaired")
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(calls[0]), 2)
        self.assertEqual(len(result.trials[0]["witnesses"]), 1)
        self.assertNotEqual(result.final_memory, transition().proposed_memory)

    def test_defer_on_failed_support_check(self):
        result = TransitionGuard("B4", reviewers("reject_repair")).review(transition(), lambda _: "restricted")
        self.assertEqual(result.commit_decision, "defer")
        self.assertEqual(result.final_memory, EMPTY_MEMORY)
        self.assertTrue(result.repair_abandoned)
        self.assertIsNotNone(result.repaired_memory)

    def test_repairer_may_abstain(self):
        result = TransitionGuard("B4", reviewers("abstain")).review(transition(), lambda _: "restricted")
        self.assertEqual(result.commit_decision, "defer")
        self.assertIsNone(result.repaired_memory)

    def test_defer_preserves_previous_state(self):
        t = transition()
        t = MemoryTransition("previous valid rule", t.candidate_experiences, t.selected_experiences,
                             t.proposed_memory, t.update_context)
        result = TransitionGuard("B4", reviewers("abstain")).review(t, lambda _: "restricted")
        self.assertEqual(result.final_memory, "previous valid rule")

    def test_change_alone_or_inconclusive_never_repairs(self):
        for mode in ("no_gap", "inconclusive", "bad_quotes", "stochastic", "malformed"):
            with self.subTest(mode=mode):
                result = TransitionGuard("B3", reviewers(mode)).review(transition(), lambda _: "different")
                self.assertEqual(result.commit_decision, "commit_original")
                self.assertFalse(result.evidence_gap)
                self.assertEqual(result.final_memory, transition().proposed_memory)

    def test_verifier_cannot_claim_change_when_replay_is_identical(self):
        result = TransitionGuard("B3", reviewers()).review(transition(), lambda _: transition().proposed_memory)
        self.assertEqual(result.commit_decision, "commit_original")
        self.assertFalse(result.evidence_gap)

    def test_guided_selection_rejects_fabricated_quotes(self):
        models = reviewers()
        models["selection"].invoke = lambda *_: json.dumps({"witnesses": [{"record_id": "record_003", "quote": "fabricated quote no evidence"}]})
        result = TransitionGuard("B4", models).review(transition(), lambda _: self.fail("No valid witness"))
        self.assertEqual(result.witnesses, [])
        self.assertEqual(result.commit_decision, "commit_original")

    def test_malformed_structured_selection_is_inconclusive(self):
        models = reviewers()
        models["selection"].invoke = lambda *_: json.dumps({"witnesses": [{"record_id": ["record_003"], "quote": "malformed array identifier"}]})
        result = TransitionGuard("B4", models).review(transition(), lambda _: self.fail("Invalid selection replayed"))
        self.assertEqual(result.commit_decision, "commit_original")
        self.assertFalse(result.evidence_gap)

    def test_random_and_guided_equal_rebuild_budgets(self):
        for strategy in ("swap", "augment"):
            outcomes = []
            for group in ("B3", "B4"):
                calls = []
                def updater(items):
                    calls.append(items)
                    return "different memory"
                result = TransitionGuard(group, reviewers("no_gap"), strategy=strategy).review(transition(), updater)
                self.assertEqual(len(calls), 2)
                self.assertEqual(result.counterfactual_rebuilds, 2)
                self.assertTrue(all(len(items) == (2 if strategy == "swap" else 3) for items in calls))
                outcomes.append(result)
            self.assertEqual([x["removed"] for x in outcomes[0].trials], [x["removed"] for x in outcomes[1].trials])

    def test_random_witness_reproducibility(self):
        guard = TransitionGuard("B3", reviewers("no_gap"), seed=42)
        a = guard.review(transition(), lambda _: "replayed")
        b = guard.review(transition(), lambda _: "replayed")
        self.assertEqual(a.to_dict(), b.to_dict())

    def test_final_only_never_sees_omitted_evidence(self):
        models = reviewers("no_gap")
        TransitionGuard("B1", models).review(transition(), lambda _: self.fail("B1 must not replay"))
        prompt = json.dumps(models["final_judge"].prompts)
        for omitted in transition().omitted_experiences:
            self.assertNotIn(omitted.text, prompt)
        self.assertFalse(models["selection"].prompts)

    def test_disabled_guard_does_nothing(self):
        result = TransitionGuard("B0", {}).review(transition(), lambda _: self.fail("Disabled updater called"))
        self.assertEqual(result.final_memory, transition().proposed_memory)
        self.assertEqual(result.commit_decision, "commit_original")


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mcq = read_jsonl(ROOT / "outputs/mcq_item_audit/accepted_cases.jsonl")
        cls.p4 = [c for c in cls.mcq if c["interface"] == "P4"]

    def test_allowlist_removes_all_gold_metadata(self):
        case = copy.deepcopy(self.p4[0])
        marker = "FORBIDDEN_BENCHMARK_METADATA"
        for field in ("clean_policy", "poisoned_policy", "scope", "metadata", "evaluation", "attack_type"):
            case[field] = marker
        case["memory_building"]["oracle_memory"] = marker
        for exp in case["candidate_experiences"]:
            for key in ("scope_bucket", "supports", "provenance", "success"):
                exp[key] = marker
        t, updater = p4_transition(case, case["memory_building"]["clean_condition"]["order"], "candidate", ProtocolLLM(), 120)
        payload = json.dumps(asdict(t))
        self.assertNotIn(marker, payload)
        for key in ("clean_answer", "poisoned_answer", "oracle_memory", "scope_bucket", "supports", "provenance", "family_id", "case_id"):
            self.assertNotIn('"' + key + '"', payload)
        for exp in case["candidate_experiences"]:
            self.assertNotIn('"' + exp["id"] + '"', payload)
        models = reviewers("no_gap")
        TransitionGuard("B4", models).review(t, updater)
        self.assertNotIn(marker, json.dumps([m.prompts for m in models.values()]))
        for model in models.values():
            for system, payload in model.prompts:
                self.assertIsNone(re.search(r"\bp[1-5]\b", system, re.I))

    def test_original_p4_and_guard_off_have_identical_invocations(self):
        a, b = ProtocolLLM(), ProtocolLLM()
        args = (None, None, None, self.p4[0], "learned", 120, "not_applicable", False)
        original = run_case(a, a, *args)
        disabled = run_case(b, b, *args, post_consolidation_guard=None)
        self.assertEqual(original, disabled)
        self.assertEqual(a.calls, b.calls)
        self.assertEqual(len(a.calls), 7)  # original full-pool mechanism control included

    def test_b2_exactly_reuses_original_full_pool_control(self):
        original = ProtocolLLM()
        _, _, _, controls = build_learned_memories(original, self.p4[0], 120, "not_applicable")
        changed = ProtocolLLM()
        clean, poisoned, traces, _ = build_learned_memories(
            changed, self.p4[0], 120, "not_applicable", build_mechanism_controls=False,
            post_consolidation_guard=TransitionGuard("B2", {}))
        self.assertEqual(clean, controls["full_pool"]["memory"])
        self.assertEqual(poisoned, controls["full_pool"]["memory"])
        self.assertEqual(traces["post_consolidation"]["clean"]["status"], "full_pool_replay")

    def test_adapter_replay_rejects_changed_or_duplicated_evidence(self):
        t, updater = p4_transition(self.p4[0], self.p4[0]["memory_building"]["clean_condition"]["order"],
                                   "proposed", ProtocolLLM(), 120)
        e = t.selected_experiences[0]
        for invalid in ((Evidence(e.record_id, "invented observation"),), (e, e)):
            with self.assertRaises(ValueError):
                updater(invalid)

    def test_freeform_guard_is_explicitly_unsupported(self):
        case = read_jsonl(ROOT / "outputs/freeform_item_audit/accepted_cases.jsonl")[0]
        llm = ProtocolLLM()
        with self.assertRaises(ValueError):
            run_case(llm, llm, None, None, None, case, "learned", 120, "not_applicable", False,
                     post_consolidation_guard=TransitionGuard("B4", {}))

    def test_deferred_update_still_runs_original_downstream_agent(self):
        llm = ProtocolLLM()
        row = run_case(llm, llm, None, None, None, self.p4[0], "learned", 120, "not_applicable", False,
                       post_consolidation_guard=TransitionGuard("B4", reviewers("reject_repair")),
                       run_mechanism_controls=False)
        for side in ("clean", "poisoned"):
            self.assertEqual(row["learned_memories"][side], EMPTY_MEMORY)
            self.assertEqual(row["memory_traces"]["post_consolidation"][side]["commit_decision"], "defer")
            self.assertTrue(row["task_results"][0]["learned_" + side]["parse_success"])
        self.assertTrue(any(EMPTY_MEMORY in user and "FINAL_ANSWER" in system for system, user in llm.calls))

    def test_original_interfaces_and_freeform_imports(self):
        for module in ("memform.eval.core", "memform.eval.langchain", "memform.eval.llamaindex", "memform.eval.defense", "memform.freeform.evaluator"):
            importlib.import_module(module)
        for interface in ("P1", "P2", "P3", "P4", "P5"):
            case = next(c for c in self.mcq if c["interface"] == interface)
            llm = ProtocolLLM()
            row = run_case(llm, llm, None, None, None, case, "learned", 120, "incremental_merge", False)
            self.assertTrue(row["task_results"])
            if interface != "P4":
                with self.assertRaises(ValueError):
                    run_case(llm, llm, None, None, None, case, "learned", 120, "incremental_merge", False,
                             post_consolidation_guard=TransitionGuard("B4", {}))
        self.assertTrue(all(not validate_compiled_case(c) for c in self.mcq))
        freeform = read_jsonl(ROOT / "outputs/freeform_item_audit/accepted_cases.jsonl")
        self.assertTrue(all(not validate_freeform_case_for_evaluation(c) for c in freeform))

    def test_fixed_eligibility_does_not_shrink_after_clean_damage(self):
        row = {"interface": "P4", "case_id": "example", "task_results": [{
            "task_id": "task", "clean_answer": "A", "poisoned_answer": "B",
            "baseline": {"predicted_answer": "A"},
            "learned_clean": {"predicted_answer": "A"}, "learned_poisoned": {"predicted_answer": "B"}}]}
        eligibility = build_eligibility(index_case_rows([row]), ["P4"])
        defended = copy.deepcopy(row)
        defended["task_results"][0]["learned_clean"]["predicted_answer"] = "B"
        score = score_on_eligibility(index_case_rows([defended]), eligibility, ["P4"])["by_interface"]["P4"]
        self.assertEqual(score["n_eligible"], 1)
        self.assertEqual(score["learned_clean_accuracy"], 0)
        self.assertEqual(score["causal_asr"], 1)

    def test_fixed_sample_selection(self):
        selected = select_cases(self.p4, 16, 42)
        self.assertEqual(selected, select_cases(list(reversed(self.p4)), 16, 42))
        self.assertEqual(len({c["family_id"] for c in selected}), 16)
        self.assertEqual(len(select_cases(self.p4, 0, 42)), len(self.p4))

    def test_actual_dataset_mock_five_groups_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "run"
            command = [sys.executable, "-B", "-m", "experiments.transition_guard_p4.run_experiment", "--mode", "mock",
                       "--groups", "B0", "B1", "B2", "B3", "B4",
                       "--sample-size", "16", "--limit", "2", "--output-dir", str(out), "--cache", str(Path(temp) / "cache.jsonl")]
            result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads((out / "summary.json").read_text())
            rows = read_jsonl(out / "case_results.jsonl")
            self.assertEqual(len(rows), 10)
            self.assertEqual({r["experiment_group"] for r in rows}, {"B0", "B1", "B2", "B3", "B4"})
            self.assertTrue(summary["complete"])
            self.assertTrue(summary["synthetic_results"])
            self.assertEqual(summary["total_cost"]["api_attempts"], 0)
            self.assertIsNone(summary["total_cost"]["input_tokens"])
            write_jsonl(out / "case_results.jsonl", [r for r in rows if not (r["experiment_group"] == "B4" and r["case_id"] == rows[0]["case_id"])])
            incomplete = analyze_directory(out)
            self.assertFalse(incomplete["groups"]["B4"]["results_complete"])
            self.assertIsNone(incomplete["groups"]["B4"]["fixed_metrics"]["causal_asr"])
            invalid_rows = copy.deepcopy(rows)
            invalid_row = next(r for r in invalid_rows if r["experiment_group"] == "B4")
            invalid_row["task_results"][0]["learned_poisoned"]["parse_success"] = False
            write_jsonl(out / "case_results.jsonl", invalid_rows)
            invalid_summary = analyze_directory(out)
            self.assertIsNone(invalid_summary["groups"]["B4"]["fixed_metrics"]["causal_asr"])
            self.assertIn(invalid_row["case_id"], invalid_summary["groups"]["B4"]["invalid_output_case_ids"])
            write_jsonl(out / "case_results.jsonl", [r for r in rows if r["experiment_group"] != "B0"])
            no_baseline = analyze_directory(out)
            self.assertFalse(no_baseline["eligibility_final"])
            self.assertTrue(all(g["fixed_metrics"]["causal_asr"] is None for g in no_baseline["groups"].values()))
            retry = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            self.assertNotEqual(retry.returncode, 0)


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.cache = self.root / "cache.jsonl"
        self.common = [sys.executable, "-B", "-m", "experiments.transition_guard_p4.run_experiment",
                       "--groups", "B0", "B1", "B2", "B3", "B4",
                       "--mode", "mock", "--model", "test-model", "--sample-size", "16", "--limit", "2",
                       "--cache", str(self.cache)]
        result = subprocess.run(self.common + ["--output-dir", str(self.source)], cwd=ROOT,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.original = read_jsonl(self.source / "case_results.jsonl")

    def resume(self, destination, extra=()):
        return subprocess.run(self.common + ["--resume-from", str(self.source), "--output-dir", str(destination)] + list(extra),
                              cwd=ROOT, capture_output=True, text=True)

    def test_only_missing_cells_execute_and_successes_are_preserved(self):
        removed = {(self.original[6]["experiment_group"], self.original[6]["case_id"]),
                   (self.original[9]["experiment_group"], self.original[9]["case_id"])}
        kept = [r for r in self.original if (r['experiment_group'], r['case_id']) not in removed]
        write_jsonl(self.source / "case_results.jsonl", kept)
        write_jsonl(self.source / "errors.jsonl", [{"group": g, "case_id": cid, "error_type": "SyntheticConnectionError"}
                                                   for g, cid in sorted(removed)])
        destination = self.root / "resumed"
        result = self.resume(destination)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("retained=8 pending=2", result.stdout)
        self.assertEqual(result.stdout.count(" reused"), 8)
        self.assertEqual(result.stdout.count(" ok"), 2)
        rows = read_jsonl(destination / "case_results.jsonl")
        summary = json.loads((destination / "summary.json").read_text())
        self.assertEqual(len(rows), 10)
        self.assertEqual(len({(r['experiment_group'], r['case_id']) for r in rows}), 10)
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["historical_failure_count"], 2)
        self.assertEqual(summary["errors"], [])
        self.assertEqual(summary["new_run_cost"]["simulated_generations"], 0)
        self.assertGreater(summary["new_run_cost"]["logical_invocations"], 0)
        for row in kept:
            copied = next(r for r in rows if r['experiment_group'] == row['experiment_group'] and r['case_id'] == row['case_id'])
            self.assertEqual(copied["task_results"], row["task_results"])
            self.assertEqual(copied["transition_reviews"], row["transition_reviews"])
            self.assertEqual(copied["call_event_range"], row["call_event_range"])
        self.assertEqual(read_jsonl(self.source / "case_results.jsonl"), kept)
        old_summary = json.loads((self.source / "summary.json").read_text())
        self.assertEqual(summary["eligibility_keys"], old_summary["eligibility_keys"])
        events = read_jsonl(destination / "call_events.jsonl")
        self.assertEqual([e['event_id'] for e in events], list(range(len(events))))
        self.assertEqual(summary["cost_scope"], "resume_source_chain_plus_current")
        third = self.root / 'resumed_again'
        again = self.resume(third, ['--resume-from', str(destination)])
        self.assertEqual(again.returncode, 0, again.stderr)
        chained = json.loads((third / 'summary.json').read_text())
        self.assertEqual(chained['total_cost'], summary['total_cost'])
        self.assertEqual(chained['new_run_cost']['logical_invocations'], 0)
        self.assertEqual(chained['historical_failure_count'], 2)

    def test_fully_complete_resume_makes_no_model_invocations(self):
        destination = self.root / "resumed"
        result = self.resume(destination)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("retained=10 pending=0", result.stdout)
        self.assertEqual(result.stdout.count(" reused"), 10)
        self.assertNotIn(" ok", result.stdout)
        summary = json.loads((destination / "summary.json").read_text())
        self.assertEqual(summary["new_run_cost"]["logical_invocations"], 0)
        self.assertEqual(summary["new_run_cost"]["api_attempts"], 0)
        self.assertTrue(summary["complete"])

    def test_changed_experiment_is_rejected_before_creating_output(self):
        destination = self.root / "mismatch"
        result = self.resume(destination, ["--strategy", "augment"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Resume configuration mismatch: strategy", result.stderr)
        self.assertFalse(destination.exists())

    def test_invalid_output_is_reexecuted_without_selecting_on_correctness(self):
        rows = copy.deepcopy(self.original)
        rows[-1]["task_results"][0]["learned_poisoned"]["parse_success"] = False
        write_jsonl(self.source / "case_results.jsonl", rows)
        destination = self.root / "resumed"
        result = self.resume(destination)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("retained=9 pending=1", result.stdout)
        self.assertEqual(result.stdout.count(" ok"), 1)
        restored = read_jsonl(destination / "case_results.jsonl")
        self.assertTrue(all(t[c]['parse_success'] for r in restored for t in r['task_results']
                            for c in ['baseline', 'learned_clean', 'learned_poisoned']))

    def test_missing_baseline_is_rebuilt_before_retained_defenses(self):
        kept = [r for r in self.original if r['experiment_group'] != 'B0']
        for row in kept:
            row['original_task_results'] = None
            row['baseline_available'] = False
        write_jsonl(self.source / 'case_results.jsonl', kept)
        destination = self.root / 'resumed'
        result = self.resume(destination)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('retained=8 pending=2', result.stdout)
        rows = read_jsonl(destination / 'case_results.jsonl')
        self.assertTrue(all(r['baseline_available'] and r['original_task_results'] is not None for r in rows))
        self.assertTrue(json.loads((destination / 'summary.json').read_text())['complete'])

    def test_validate_resume_only_writes_a_plan(self):
        destination = self.root / 'plan'
        result = self.resume(destination, ['--mode', 'validate'])
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads((destination / 'resume_plan.json').read_text())
        self.assertEqual(plan['retained_cells'], 10)
        self.assertEqual(plan['pending_cells'], 0)
        self.assertFalse((destination / 'call_events.jsonl').exists())
        self.assertFalse((destination / 'case_results.jsonl').exists())


class CacheTelemetryTests(unittest.TestCase):
    def test_exception_chain_records_types_without_sensitive_messages(self):
        class FailingBackend:
            cache_hits = 0
            last_call_usage = None
            def invoke(self, system, user):
                try:
                    raise OSError("SECRET_TOKEN_MUST_NOT_APPEAR")
                except OSError as exc:
                    raise RuntimeError("SECRET_TOKEN_MUST_NOT_APPEAR") from exc
        with tempfile.TemporaryDirectory() as temp:
            events = []
            model = TrackedModel(FailingBackend(), "memory", events, {}, Path(temp) / 'events.jsonl',
                                 mode='live', model='test-model')
            with self.assertRaises(RuntimeError):
                model.invoke('system', 'user')
            self.assertEqual(events[0]['error_chain'], ['RuntimeError', 'OSError'])
            self.assertNotIn('SECRET_TOKEN_MUST_NOT_APPEAR', json.dumps(events))

    def test_incomplete_usage_and_cache_hits_are_not_fabricated_spend(self):
        common = dict(mode="live", stage="memory", latency_seconds=0.1, simulated_generation=False)
        events = [dict(common, api_attempts=1, success=True, cache_hit=False,
                       usage={"input_tokens": 20, "output_tokens": 5}),
                  dict(common, api_attempts=0, success=True, cache_hit=True, usage=None),
                  dict(common, api_attempts=1, success=False, cache_hit=False, usage=None)]
        cost = summarize_cost(events)
        self.assertEqual(cost["api_attempts"], 2)
        self.assertEqual(cost["cache_hits"], 1)
        self.assertIsNone(cost["input_tokens"])
        self.assertEqual(cost["known_input_tokens"], 20)
        self.assertEqual(cost["api_responses_without_usage"], 1)

    def test_usage_availability_cache_compatibility_and_no_invented_tokens(self):
        class FakeClient:
            def __init__(self, **kwargs):
                self.response = types.SimpleNamespace(content="a policy", usage_metadata=None)
            def invoke(self, messages):
                return self.response
        with tempfile.TemporaryDirectory() as temp, patch.dict(sys.modules, {"langchain_openai": types.SimpleNamespace(ChatOpenAI=FakeClient)}):
            cache = DiskCache(Path(temp) / "cache.jsonl")
            model = CachedChatModel("memory", "fake", None, "test-placeholder", cache, timeout=None, max_retries=0)
            model.invoke("system", "first")
            self.assertIsNone(model.last_call_usage)
            model.client.response.usage_metadata = {"input_tokens": 20, "output_tokens": 5}
            model.invoke("system", "second")
            self.assertEqual(model.last_call_usage, {"input_tokens": 20, "output_tokens": 5})
            reloaded = DiskCache(cache.path)
            replay = CachedChatModel("memory", "fake", None, None, reloaded, timeout=None, cache_only=True)
            replay.invoke("system", "second")
            self.assertEqual(replay.last_call_usage, {"input_tokens": 20, "output_tokens": 5})
            replay.invoke("system", "first")
            self.assertIsNone(replay.last_call_usage)
            self.assertEqual(replay.real_calls, 0)


if __name__ == "__main__":
    unittest.main()
