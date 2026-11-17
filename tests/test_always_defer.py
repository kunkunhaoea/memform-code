"""Always-defer must use real fallback prompts without Updater/guard calls."""
from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from test_transition_guard import ROOT, ProtocolLLM, transition
from memform.common import read_jsonl
from memform.defenses.common import EMPTY_MEMORY
from memform.defenses.transition_guard import TransitionGuard
from memform.eval.core import run_case
from experiments.transition_guard_p4.analyze_results import compare_always_defer


class NeverModel:
    def invoke(self, *args):
        raise AssertionError("Always-defer must not call an Updater or defense model")


class AlwaysDeferTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.case = next(c for c in read_jsonl(ROOT / "outputs/mcq_item_audit/accepted_cases.jsonl")
                        if c["interface"] == "P4")

    def test_bypass_matches_post_consolidation_defer_agent_prompts(self):
        agent, reference = ProtocolLLM(), ProtocolLLM()
        args = (None, None, None, self.case, "learned", 120, "not_applicable", False)
        actual = run_case(agent, NeverModel(), *args, always_defer=True, run_mechanism_controls=False)
        expected = run_case(reference, ProtocolLLM(), *args,
                            post_consolidation_guard=TransitionGuard("B5", {}), run_mechanism_controls=False)
        self.assertEqual(agent.calls, reference.calls)
        self.assertEqual(actual["task_results"], expected["task_results"])
        self.assertEqual(actual["learned_memories"]["clean"], EMPTY_MEMORY)
        self.assertEqual(actual["learned_memories"]["poisoned"], EMPTY_MEMORY)
        self.assertTrue(actual["memory_traces"]["memory_update_bypassed"])
        self.assertEqual(len(agent.calls), 3)
        self.assertEqual(agent.calls[1], agent.calls[2])
        self.assertNotEqual(agent.calls[0], agent.calls[1])

    def test_guard_control_preserves_previous_state_without_verification(self):
        t = replace(transition(), previous_memory="Previously committed policy.")
        result = TransitionGuard("B5", {}).review(t, NeverModel().invoke)
        self.assertEqual(result.final_memory, t.previous_memory)
        self.assertEqual(result.status, "always_defer")
        self.assertEqual(result.commit_decision, "defer")
        self.assertFalse(result.review_triggered)
        self.assertFalse(result.repair_attempted)
        self.assertFalse(result.evidence_gap)

    def test_bypass_rejects_incompatible_modes_before_model_calls(self):
        args = (None, None, None, self.case, "learned", 120, "not_applicable", False)
        for kwargs in ({"run_mechanism_controls": True},
                       {"run_mechanism_controls": False, "post_consolidation_guard": TransitionGuard("B4", {})},
                       {"run_mechanism_controls": False, "defense": object()}):
            with self.subTest(kwargs=list(kwargs)), self.assertRaisesRegex(ValueError, "Always-defer requires"):
                run_case(NeverModel(), NeverModel(), *args, always_defer=True, **kwargs)

    def test_cli_six_groups_and_resume_keep_b5_agent_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            base = [sys.executable, "-B", "-m", "experiments.transition_guard_p4.run_experiment",
                    "--mode", "mock", "--sample-size", "2", "--groups", "B0", "B1", "B2", "B3", "B4", "B5",
                    "--cache", str(root / "cache.jsonl")]
            first = subprocess.run(base + ["--output-dir", str(source)], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            rows = read_jsonl(source / "case_results.jsonl")
            self.assertEqual(len(rows), 12)
            baseline = {r["case_id"]: r for r in rows if r["experiment_group"] == "B0"}
            for row in (r for r in rows if r["experiment_group"] == "B5"):
                self.assertEqual(row["candidate_memory_source"], "paired_B0")
                for side, review in row["transition_reviews"].items():
                    self.assertEqual(review["original_memory"], baseline[row["case_id"]]["learned_memories"][side])
                    self.assertEqual(review["final_memory"], EMPTY_MEMORY)
                    self.assertEqual(review["commit_decision"], "defer")
                    self.assertFalse(review["review_triggered"])
            events = [e for e in read_jsonl(source / "call_events.jsonl") if e["group"] == "B5"]
            self.assertEqual({e["role"] for e in events}, {"agent"})
            summary = json.loads((source / "summary.json").read_text())
            self.assertTrue(summary["complete"])
            self.assertTrue(summary["groups"]["B4"]["always_defer_comparison"]["complete"])
            self.assertEqual(summary["groups"]["B5"]["transitions"]["defer_rate"], 1)
            self.assertNotIn("adapter_pool_records_enumerated", summary["groups"]["B5"]["transitions"]["totals"])
            self.assertIn("Wins vs B5", (source / "comparison.md").read_text())
            resumed = root / "resumed"
            again = subprocess.run(base + ["--resume-from", str(source), "--output-dir", str(resumed)],
                                   cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertIn("retained=12 pending=0", again.stdout)
            self.assertEqual(json.loads((resumed / "summary.json").read_text())["new_run_cost"]["logical_invocations"], 0)

    def test_b5_only_needs_no_candidate_and_reuses_empty_prompt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            out = root / "b5"
            proc = subprocess.run([sys.executable, "-B", "-m", "experiments.transition_guard_p4.run_experiment",
                                   "--mode", "mock", "--groups", "B5", "--sample-size", "2",
                                   "--cache", str(root / "cache.jsonl"), "--output-dir", str(out)],
                                  cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            rows = read_jsonl(out / "case_results.jsonl")
            self.assertTrue(all(r["candidate_memory_source"] == "not_generated" for r in rows))
            self.assertTrue(all(v["original_memory"] == "" for r in rows for v in r["transition_reviews"].values()))
            events = read_jsonl(out / "call_events.jsonl")
            self.assertEqual({e["role"] for e in events}, {"agent"})
            self.assertEqual(len(events), 6)
            self.assertEqual(sum(e["simulated_generation"] for e in events), 4)
            summary = json.loads((out / "summary.json").read_text())
            self.assertIsNone(summary["groups"]["B5"]["fixed_metrics"]["causal_asr"])
            self.assertTrue(summary["groups"]["B5"]["always_defer_comparison"]["complete"])


class PairedControlTests(unittest.TestCase):
    @staticmethod
    def row(name, clean, poisoned):
        return {"case_id": name, "task_results": [{
            "task_id": name + "_T", "clean_answer": "A",
            "baseline": {"parse_success": True},
            "learned_clean": {"parse_success": True, "clean_correct": clean},
            "learned_poisoned": {"parse_success": True, "clean_correct": poisoned}}]}

    def test_paired_wins_losses_include_cases_outside_eligibility(self):
        control = [self.row("a", False, False), self.row("b", True, True)]
        current = [self.row("a", True, True), self.row("b", True, False)]
        result = compare_always_defer(current, control, {"a", "b"})
        self.assertTrue(result["complete"])
        self.assertEqual(result["scope"], "all_selected_tasks")
        self.assertEqual(result["by_condition"]["clean"]["accuracy_delta"], .5)
        self.assertEqual(result["by_condition"]["poisoned"]["wins"], 1)
        self.assertEqual(result["by_condition"]["poisoned"]["losses"], 1)
        self.assertEqual(result["by_condition"]["poisoned"]["accuracy_delta"], 0)

    def test_missing_or_invalid_control_cannot_show_a_complete_gain(self):
        current = [self.row("a", True, True), self.row("b", True, True)]
        invalid = copy.deepcopy(current)
        invalid[1]["task_results"][0]["learned_clean"]["parse_success"] = False
        for control in (current[:1], invalid):
            result = compare_always_defer(current, control, {"a", "b"})
            self.assertFalse(result["complete"])
            self.assertIsNone(result["by_condition"]["poisoned"]["accuracy_delta"])
            self.assertIsNone(result["by_condition"]["clean"]["wins"])

    def test_changed_labels_cannot_be_paired(self):
        row = self.row("a", True, True)
        other = copy.deepcopy(row)
        other["task_results"][0]["clean_answer"] = "B"
        with self.assertRaisesRegex(ValueError, "Scoring label mismatch"):
            compare_always_defer([row], [other], {"a"})


if __name__ == "__main__":
    unittest.main()
