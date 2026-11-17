"""V2 regression tests: evidence coverage, edit auditing and bounded contexts."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import types
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from test_transition_guard import transition, reviewers, ROOT
from memform.defenses.common import EMPTY_MEMORY
from memform.defenses.transition_guard import Evidence, TransitionGuard
from memform.defenses.transition_guard.retrieval import shortlist
from memform.eval.core import CachedChatModel, DiskCache, CacheOnlyMiss
from experiments.transition_guard_p4.runtime import make_models


class GuardV2Tests(unittest.TestCase):
    def audit_with(self, mutate, *, stage="repair_check", k=1):
        models = reviewers()
        original = models[stage].invoke
        def invoke(system, user):
            obj = json.loads(original(system, user))
            mutate(obj)
            return json.dumps(obj)
        models[stage].invoke = invoke
        result = TransitionGuard("B4", models, k=k, max_rebuilds=2 // k).review(transition(), lambda _: "A restricted policy.")
        return result, models

    def test_all_trials_precede_single_repair_and_all_witnesses_reach_audit(self):
        models = reviewers()
        order = []
        for stage, model in models.items():
            original = model.invoke
            def invoke(system, user, stage=stage, original=original):
                order.append(stage)
                return original(system, user)
            model.invoke = invoke
        r = TransitionGuard("B4", models).review(transition(), lambda _: "A restricted policy.")
        self.assertEqual(order, ["selection", "verification", "verification", "repair", "repair_check"])
        self.assertEqual(r.commit_decision, "commit_repaired")
        check = models["repair_check"].prompts[0][1]
        self.assertEqual(set(check["required_witness_ids"]), {e.record_id for e in transition().omitted_experiences})
        self.assertNotIn("review", check)
        self.assertNotIn("confirmed_defects", check)
        self.assertNotIn("effect", check["edits"][0])
        self.assertEqual(len(models["repair"].prompts[0][1]["confirmed_defects"]), 2)

    def test_second_witness_conflict_rejects_first_witness_repair(self):
        def mutate(obj):
            obj["witness_checks"][-1]["status"] = "conflict"
            obj["witness_checks"][-1]["reason"] = "A remaining action contradicts this observation."
        result, _ = self.audit_with(mutate)
        self.assertEqual(result.commit_decision, "defer")
        self.assertEqual(result.final_memory, EMPTY_MEMORY)

    def test_unchanged_updater_does_not_hide_directly_evidenced_defect(self):
        models = reviewers()
        original = models["verification"].invoke
        def invoke(system, user):
            obj = json.loads(original(system, user))
            obj.update(material_change=False, counterfactual_better_supported=False)
            return json.dumps(obj)
        models["verification"].invoke = invoke
        result = TransitionGuard("B4", models).review(transition(), lambda _: transition().proposed_memory)
        self.assertEqual(result.commit_decision, "commit_repaired")
        self.assertTrue(all(not x["verification"]["counterfactual_improved"] for x in result.trials))

    def test_defect_must_quote_actual_original_claim(self):
        result, _ = self.audit_with(lambda o: o.update(defective_claim="A claim absent from the original memory."), stage="verification")
        self.assertEqual(result.commit_decision, "commit_original")

    def test_no_unaudited_rewrite_or_invalid_patch_is_committed(self):
        mutations = [
            lambda o: o.update(edits=[], memory="A wholly new policy"),
            lambda o: o["edits"][0].update(old="Nonexistent original sentence."),
            lambda o: o["edits"].append(dict(o["edits"][0])),
            lambda o: o["edits"][0].update(new=o["edits"][0]["old"]),
            lambda o: o["edits"][0].update(effect=["restrict"]),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                result, models = self.audit_with(mutate, stage="repair")
                self.assertEqual(result.commit_decision, "defer")
                self.assertFalse(models["repair_check"].prompts)

    def test_single_record_cannot_authorize_expanded_rule(self):
        def mutate(obj):
            obj["edit_checks"][0].update(effect="expand", introduces_permission=True,
                                         citations=obj["edit_checks"][0]["citations"][:1])
        result, models = self.audit_with(mutate)
        self.assertEqual(result.commit_decision, "defer")
        self.assertTrue(models["repair_check"].prompts)

    def test_audit_rejects_conflicts_unknown_collapse_and_misclassified_edits(self):
        mutations = [
            lambda o: next(c for c in o["checks"] if c["check"] == "policy_consistency").update(status="unknown"),
            lambda o: next(c for c in o["checks"] if c["check"] == "unknown_handling").update(status="unknown"),
            lambda o: next(c for c in o["checks"] if c["check"] == "permission_scope").update(status="unknown"),
            lambda o: o.update(witness_checks=o["witness_checks"][:1]),
            lambda o: o["edit_checks"][0].update(citations=[]),
            lambda o: o["edit_checks"][0].update(index=True),
            lambda o: o["edit_checks"][0].update(status=["supported"]),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                result, _ = self.audit_with(mutate)
                self.assertEqual(result.commit_decision, "defer")

    def test_empty_or_malformed_selection_schema_is_inconclusive(self):
        for reply in ({}, {"witnesses": "not a list"}, {"witnesses": [None]}):
            models = reviewers()
            models["selection"].invoke = lambda *_, reply=reply: json.dumps(reply)
            result = TransitionGuard("B4", models).review(transition(), lambda _: self.fail("No replay expected"))
            self.assertEqual(result.status, "inconclusive")

    def test_context_overflow_preserves_previous_memory_without_api(self):
        models = reviewers()
        t = replace(transition(), previous_memory="previous policy")
        result = TransitionGuard("B4", models, guard_max_input_chars=100).review(t, lambda _: self.fail("No replay expected"))
        self.assertEqual(result.status, "context_budget_exceeded")
        self.assertEqual(result.final_memory, "previous policy")
        self.assertFalse(any(m.prompts for m in models.values()))

    def test_cf_replay_is_bounded_before_updater_call(self):
        t = transition()
        large = Evidence("large", "A" * 30000)
        t = replace(t, candidate_experiences=t.candidate_experiences + (large,),
                    selected_experiences=t.selected_experiences + (large,))
        result = TransitionGuard("B3", reviewers(), strategy="augment").review(t, lambda _: self.fail("Oversized replay"))
        self.assertEqual(result.status, "context_budget_exceeded")

    def test_shortlist_empty_from_budget_is_defer_not_no_witness(self):
        result = TransitionGuard("B4", reviewers(), selection_max_chars=10).review(transition(), lambda _: self.fail("No replay expected"))
        self.assertEqual(result.status, "retrieval_budget_exhausted")

    def test_k_two_uses_one_replay_with_all_witnesses(self):
        result, _ = self.audit_with(lambda _: None, k=2)
        self.assertEqual(result.counterfactual_rebuilds, 1)
        self.assertEqual(result.commit_decision, "commit_repaired")
        self.assertEqual(len(result.trials[0]["witnesses"]), 2)


class RetrievalV2Tests(unittest.TestCase):
    def test_large_pool_has_bounded_whole_record_llm_shortlist(self):
        t = transition()
        background = tuple(Evidence(f"background_{i:04d}", "Unrelated calendar decoration event.") for i in range(1000))
        t = replace(t, candidate_experiences=t.candidate_experiences + background)
        models = reviewers("no_gap")
        r = TransitionGuard("B4", models, selection_max_records=3, selection_max_chars=500).review(t, lambda _: "Replayed policy")
        selection = models["selection"].prompts[0][1]
        self.assertLessEqual(len(selection["omitted"]), 3)
        self.assertLessEqual(len(json.dumps(selection["omitted"], ensure_ascii=False)), 500)
        pool = {e.record_id: e.text for e in t.omitted_experiences}
        self.assertTrue(all(pool[e["record_id"]] == e["text"] for e in selection["omitted"]))
        self.assertEqual(r.selection["local_records_scanned"], 1002)
        self.assertLessEqual(r.selection["selection_scan_count"], 3)

    def test_retrieval_ranks_relevance_and_skips_oversized_without_truncation(self):
        omitted = (Evidence("a", "Unrelated calendars and decorations."),
                   Evidence("b", "Restart monitoring agent: checkpoint record unavailable."),
                   Evidence("c", "checkpoint " * 10000))
        chosen, info = shortlist(omitted, "checkpoint unavailable monitoring agent", max_records=1, max_chars=200)
        self.assertEqual([e.record_id for e in chosen], ["b"])
        self.assertEqual(info["oversized_records_skipped"], 1)
        reverse, _ = shortlist(tuple(reversed(omitted)), "checkpoint unavailable monitoring agent", max_records=1, max_chars=200)
        self.assertEqual(chosen, reverse)


class OutputBudgetV2Tests(unittest.TestCase):
    def test_output_cap_reaches_guard_only_and_changes_cache_identity(self):
        class FakeClient:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
            def invoke(self, messages):
                return types.SimpleNamespace(content="policy", usage_metadata=None)
        with tempfile.TemporaryDirectory() as temp, patch.dict(sys.modules, {"langchain_openai": types.SimpleNamespace(ChatOpenAI=FakeClient)}):
            cache = DiskCache(Path(temp) / "cache.jsonl")
            models = make_models(mode="live", model="fake", guard_model="fake", base_url=None,
                                 api_key="placeholder", cache=cache, events=[], scope={},
                                 event_path=Path(temp) / "events.jsonl", timeout=None,
                                 temperature=0, guard_max_output_tokens=4096, guard_reasoning_effort="low")
            for role, tracked in models.items():
                self.assertEqual(tracked.backend.client.kwargs.get("max_tokens"),
                                 None if role in {"memory", "agent"} else 4096)
                self.assertEqual(tracked.backend.client.kwargs.get("reasoning_effort"),
                                 None if role in {"memory", "agent"} else "low")
            models["memory"].backend.invoke("system", "user")
            capped = CachedChatModel("memory", "fake", None, None, cache, cache_only=True, max_output_tokens=4096)
            with self.assertRaises(CacheOnlyMiss):
                capped.invoke("system", "user")

    def test_resume_rejects_budget_or_source_version_change(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source"
            base = [sys.executable, "-B", "-m", "experiments.transition_guard_p4.run_experiment",
                    "--mode", "mock", "--sample-size", "16", "--limit", "1", "--cache", str(Path(temp) / "cache.jsonl")]
            first = subprocess.run(base + ["--output-dir", str(source)], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            for flag, value in [("--selection-max-records", "3"), ("--guard-max-output-tokens", "4096")]:
                dest = Path(temp) / flag[2:]
                result = subprocess.run(base + ["--resume-from", str(source), "--output-dir", str(dest), flag, value], cwd=ROOT, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Resume configuration mismatch", result.stderr)
                self.assertFalse(dest.exists())
            config = json.loads((source / "run_config.json").read_text())
            config["implementation_sha256"] = "obsolete-source"
            (source / "run_config.json").write_text(json.dumps(config))
            result = subprocess.run(base + ["--resume-from", str(source), "--output-dir", str(Path(temp) / "changed")], cwd=ROOT, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("implementation_sha256", result.stderr)


if __name__ == "__main__":
    unittest.main()
