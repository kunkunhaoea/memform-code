"""Cheap regressions for v3; real provider behavior is deliberately not sampled."""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_transition_guard import ROOT, reviewers, transition
from memform.defenses.transition_guard import TransitionGuard
from memform.llm_compat import output_token_field, provider_name
from experiments.transition_guard_p4.check_output_limit import offline_probe
from experiments.transition_guard_p4.baseline import PairedGuard


class RepairV3Tests(unittest.TestCase):
    def run_guard(self, mutate=None, *, retries=1):
        models = reviewers()
        if mutate:
            original = models["repair_check"].invoke
            def invoke(system, user):
                obj = json.loads(original(system, user))
                mutate(obj, json.loads(user))
                return json.dumps(obj)
            models["repair_check"].invoke = invoke
        return TransitionGuard("B4", models, repair_length_retries=retries).review(transition(), lambda _: "A restricted policy."), models

    def test_effect_name_disagreement_alone_does_not_reject_supported_edit(self):
        result, _ = self.run_guard(lambda obj, _: obj["edit_checks"][0].update(effect="expand"))
        self.assertEqual(result.commit_decision, "commit_repaired")
        self.assertEqual(result.repair["validation"]["effect_disagreements"], 1)

    def test_independent_new_permission_gate_still_requires_two_records(self):
        def mutate(obj, _):
            obj["edit_checks"][0].update(introduces_permission=True,
                                         citations=obj["edit_checks"][0]["citations"][:1])
        result, _ = self.run_guard(mutate)
        self.assertEqual(result.commit_decision, "defer")

    def test_historical_conflict_is_allowed_with_real_uncertainty_span(self):
        models = reviewers()
        gen = models["repair"].invoke
        def repair(system, user):
            obj = json.loads(gen(system, user))
            obj["edits"][0]["new"] += " Evidence conflicts; the applicable action is uncertain."
            return json.dumps(obj)
        models["repair"].invoke = repair
        original = models["repair_check"].invoke
        def audit(system, user):
            obj = json.loads(original(system, user))
            p = json.loads(user)
            obj.update(evidence_conflicts=[{"citations": [{"record_id": e["record_id"], "quote": e["text"]}
                                                          for e in p["evidence"][:2]], "reason": "Observed actions differ"}],
                       uncertainty_quotes=["Evidence conflicts; the applicable action is uncertain."])
            obj["witness_checks"][1]["status"] = "uncertainty_preserved"
            obj["witness_checks"][1]["policy_quotes"] = obj["uncertainty_quotes"]
            return json.dumps(obj)
        models["repair_check"].invoke = audit
        result = TransitionGuard("B4", models).review(transition(), lambda _: "A restricted policy.")
        self.assertEqual(result.commit_decision, "commit_repaired")

    def test_historical_conflict_without_actual_uncertainty_text_is_rejected(self):
        for quotes in [[], ["An uncertainty sentence that never appears in the policy."]]:
            result, _ = self.run_guard(lambda o, p: o.update(evidence_conflicts=[{
                "citations": [{"record_id": e["record_id"], "quote": e["text"]} for e in p["evidence"][:2]],
                "reason": "Observed actions differ"}], uncertainty_quotes=quotes))
            self.assertEqual(result.commit_decision, "defer")

    def test_policy_conflict_cannot_be_relabelled_safe_by_other_flags(self):
        def mutate(o, p):
            next(c for c in o["checks"] if c["check"] == "policy_consistency").update(status="fail", findings=[{
                "original_quotes": [], "policy_quotes": ["Merge only when authenticated account and order match", "otherwise keep separate."],
                "citations": [{"record_id": e["record_id"], "quote": e["text"]} for e in p["evidence"]],
                "reason": "Synthetic conflicting-branch audit."}])
        result, _ = self.run_guard(mutate)
        self.assertEqual(result.commit_decision, "defer")
        self.assertEqual(result.repair["validation"]["effective_verdict"], "unsupported")

    def test_overlong_repair_has_one_shortening_call_then_fresh_audit(self):
        models = reviewers()
        original = models["repair"].invoke
        count = 0
        def repair(system, user):
            nonlocal count
            count += 1
            obj = json.loads(original(system, user))
            if count == 1:
                obj["edits"][0]["new"] += " detail" * 130
            return json.dumps(obj)
        models["repair"].invoke = repair
        result = TransitionGuard("B4", models).review(transition(), lambda _: "A restricted policy.")
        self.assertEqual(count, 2)
        self.assertIn("length_retry", result.repair)
        self.assertEqual(result.commit_decision, "commit_repaired")
        self.assertEqual(models["repair_check"].prompts[0][1]["repaired_memory"], result.final_memory)

    def test_shortening_is_bounded_and_can_be_disabled(self):
        for retry_budget in [0, 1]:
            models = reviewers()
            original = models["repair"].invoke
            count = 0
            def repair(system, user):
                nonlocal count
                count += 1
                obj = json.loads(original(system, user))
                obj["edits"][0]["new"] += " detail" * 130
                return json.dumps(obj)
            models["repair"].invoke = repair
            result = TransitionGuard("B4", models, repair_length_retries=retry_budget).review(transition(), lambda _: "A restricted policy.")
            self.assertEqual(count, 1 + retry_budget)
            self.assertEqual(result.commit_decision, "defer")
            self.assertFalse(models["repair_check"].prompts)

    def test_length_finished_or_over_budget_json_cannot_approve_repair(self):
        for meta in [{"finish_reason": "length"}, {"output_limit_exceeded": True}]:
            models = reviewers()
            models["repair_check"].last_call_metadata = meta
            result = TransitionGuard("B4", models).review(transition(), lambda _: "A restricted policy.")
            self.assertEqual(result.commit_decision, "defer")
            self.assertFalse(result.repair["support_check"]["parse_success"])


class WireV3Tests(unittest.TestCase):
    def test_real_sdk_http_body_uses_exactly_one_correct_field_for_each_provider(self):
        for base, name, field in [
            ("https://api.deepseek.com", "deepseek-flash", "max_tokens"),
            ("https://ws-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1", "deepseek-v4.1-flash-1017", "max_completion_tokens"),
            ("https://dashscope.aliyuncs.com/compatible-mode/v1", "deepseek-v4.1-flash-1017", "max_completion_tokens"),
        ]:
            probe = offline_probe(base, name)
            self.assertEqual(probe["wire_parameters"], {"model": name, field: 64})
            self.assertEqual(probe["actual_api_calls"], 0)
            self.assertEqual(probe["response_metadata"]["reasoning_tokens"], 1)

    def test_provider_detection_does_not_trust_lookalike_host_and_allows_override(self):
        self.assertEqual(provider_name("https://api.deepseek.com.evil.invalid"), "openai_compatible")
        self.assertEqual(output_token_field("https://api.deepseek.com", "max_completion_tokens"), "max_completion_tokens")


class B4OnlyV3Tests(unittest.TestCase):
    def test_changed_candidate_fails_before_guard_can_spend_tokens(self):
        class NeverRun:
            def review(self, *args):
                raise AssertionError("Guard must not run for a mismatched candidate")
        guarded = PairedGuard(NeverRun(), {"clean": "different B0 candidate", "poisoned": "another candidate"})
        with self.assertRaisesRegex(ValueError, "before guard execution"):
            guarded.review(transition(), lambda _: None)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = [sys.executable, "-B", "-m", "experiments.transition_guard_p4.run_experiment",
                     "--mode", "mock", "--model", "test-model", "--sample-size", "16", "--limit", "2",
                     "--cache", str(self.root / "cache.jsonl")]

    def run_cli(self, *extra):
        return subprocess.run(self.base + list(extra), cwd=ROOT, capture_output=True, text=True)

    def test_default_runs_only_b4_and_labels_missing_baseline_diagnostic(self):
        dest = self.root / "b4"
        proc = self.run_cli("--output-dir", str(dest))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        summary = json.loads((dest / "summary.json").read_text())
        self.assertEqual(set(summary["groups"]), {"B4"})
        self.assertTrue(summary["complete"])
        self.assertTrue(summary["diagnostic_only"])
        self.assertIsNone(summary["groups"]["B4"]["fixed_metrics"]["causal_asr"])
        self.assertIsNone(summary["groups"]["B4"]["recovery"]["rate"])
        self.assertIsNotNone(summary["groups"]["B4"]["clean_accuracy_all_selected"])
        events = [json.loads(s) for s in (dest / "call_events.jsonl").read_text().splitlines()]
        self.assertTrue(all(e["group"] == "B4" for e in events))
        again = self.run_cli("--resume-from", str(dest), "--output-dir", str(self.root / "resumed"))
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("retained=2 pending=0", again.stdout)

    def test_existing_compatible_b0_is_reused_without_other_group_calls(self):
        source = self.root / "source"
        first = self.run_cli("--groups", "B0", "B4", "--output-dir", str(source))
        self.assertEqual(first.returncode, 0, first.stderr)
        dest = self.root / "b4"
        proc = self.run_cli("--baseline-from", str(source), "--output-dir", str(dest))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        summary = json.loads((dest / "summary.json").read_text())
        self.assertTrue(summary["baseline_complete"])
        self.assertFalse(summary["diagnostic_only"])
        self.assertEqual(set(summary["groups"]), {"B4"})
        self.assertTrue((dest / "baseline_results.jsonl").exists())
        events = [json.loads(s) for s in (dest / "call_events.jsonl").read_text().splitlines()]
        self.assertTrue(all(e["group"] == "B4" for e in events))
        resumed = self.run_cli("--resume-from", str(dest), "--output-dir", str(self.root / "resumed"))
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertIn("retained=2 pending=0", resumed.stdout)

    def test_different_endpoint_or_model_cannot_reuse_baseline(self):
        source = self.root / "source"
        first = self.run_cli("--groups", "B0", "--output-dir", str(source))
        self.assertEqual(first.returncode, 0, first.stderr)
        for flag, value in [("--base-url", "https://other.example/v1"), ("--model", "other-model")]:
            dest = self.root / flag[2:]
            proc = self.run_cli("--baseline-from", str(source), flag, value, "--output-dir", str(dest))
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Baseline configuration mismatch", proc.stderr)
            self.assertFalse(dest.exists())


if __name__ == "__main__":
    unittest.main()
