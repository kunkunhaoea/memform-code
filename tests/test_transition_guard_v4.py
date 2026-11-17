"""Regressions for contradictory audits, descriptive edits and witness evidence."""
from __future__ import annotations

import copy
import json
import unittest
from dataclasses import replace

from test_transition_guard import reviewers, transition, ROOT
from memform.common import read_jsonl
from memform.defenses.common import EMPTY_MEMORY
from memform.defenses.transition_guard import TransitionGuard
from memform.defenses.transition_guard.audit import AUDIT_SCHEMA, CHECKS, compile_audit
from experiments.transition_guard_p4.analyze_results import transition_statistics


class AuditV4Tests(unittest.TestCase):
    def run_guard(self, mutate=None, repair_mutate=None, group="B4"):
        models = reviewers()
        for stage, mutation in (("repair_check", mutate), ("repair", repair_mutate)):
            if mutation:
                original = models[stage].invoke
                def invoke(system, user, original=original, mutation=mutation):
                    obj, payload = json.loads(original(system, user)), json.loads(user)
                    mutation(obj, payload)
                    return json.dumps(obj)
                models[stage].invoke = invoke
        result = TransitionGuard(group, models).review(transition(), lambda _: "A restricted policy.")
        return result, models

    @staticmethod
    def finding(payload, *, quotes=None, original=None):
        return {"policy_quotes": quotes if quotes is not None else [payload["repaired_memory"]],
                "original_quotes": original or [],
                "citations": [{"record_id": e["record_id"], "quote": e["text"]} for e in payload["evidence"][:1]],
                "reason": "Located unsupported claim in this synthetic audit."}

    def test_audit_has_no_global_verdict_or_negative_boolean_template(self):
        result, models = self.run_guard()
        system, payload = models["repair_check"].prompts[0]
        self.assertNotIn('"no_invented_facts"', system)
        self.assertNotIn('"verdict"', system)
        self.assertEqual(set(payload["required_checks"]), set(CHECKS))
        self.assertEqual(result.repair["support_check"]["parsed"]["schema_version"], AUDIT_SCHEMA)
        self.assertEqual(result.repair["validation"]["effective_verdict"], "supported")
        self.assertEqual([len(models[s].prompts) for s in ("repair", "repair_check")], [1, 1])

    def test_legacy_contradiction_is_inconclusive_never_silently_approved(self):
        result, _ = self.run_guard(lambda o, p: o.update(verdict="supported", no_invented_facts=False,
                                                       reason="No invented facts."))
        self.assertEqual(result.commit_decision, "defer")
        self.assertEqual(result.status, "repair_audit_inconclusive")
        self.assertIn("legacy_or_duplicate_decision_fields", result.repair["validation"]["protocol_errors"])

    def test_missing_duplicate_unknown_and_contradictory_checks_are_inconclusive(self):
        mutations = [lambda o, p: o.pop("schema_version"),
                     lambda o, p: o["checks"].pop(),
                     lambda o, p: o["checks"].append(copy.deepcopy(o["checks"][0])),
                     lambda o, p: o["checks"][0].update(status="unknown"),
                     lambda o, p: o["checks"][1].update(status="fail", findings=[]),
                     lambda o, p: o["checks"][1].update(findings=[self.finding(p)]),
                     lambda o, p: o["checks"][1].update(status="fail", findings=[self.finding(p, quotes=["Absent policy statement."])]),
                     lambda o, p: o["checks"][1].update(status="fail", findings=[self.finding(p, quotes=[{}])])]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                result, _ = self.run_guard(mutate)
                self.assertEqual(result.status, "repair_audit_inconclusive")
                self.assertEqual(result.final_memory, EMPTY_MEMORY)

    def test_cited_unsupported_claim_is_semantic_rejection(self):
        result, _ = self.run_guard(lambda o, p: o["checks"][1].update(status="fail", findings=[self.finding(p)]))
        self.assertEqual(result.status, "repair_not_supported")
        validation = result.repair["validation"]
        self.assertEqual(validation["effective_verdict"], "unsupported")
        self.assertFalse(validation["protocol_errors"])
        self.assertEqual(len(validation["unsupported_claims"]), 1)

    def test_unknown_is_not_a_protocol_error_or_semantic_rejection(self):
        result, _ = self.run_guard(lambda o, p: o["checks"][1].update(status="unknown"))
        self.assertEqual(result.status, "repair_audit_inconclusive")
        validation = result.repair["validation"]
        self.assertFalse(validation["protocol_errors"])
        self.assertFalse(validation["failed_checks"])
        self.assertEqual(validation["unknown_checks"], ["claim_support"])

    def test_located_witness_conflict_is_semantic_rejection(self):
        result, _ = self.run_guard(lambda o, p: o["witness_checks"][1].update(
            status="conflict", reason="The remaining policy branch conflicts with this observation."))
        self.assertEqual(result.status, "repair_not_supported")
        self.assertFalse(result.repair["validation"]["protocol_errors"])
        self.assertIn("witness:record_004", result.repair["validation"]["failed_checks"])

    def test_lost_supported_knowledge_requires_original_location(self):
        result, _ = self.run_guard(lambda o, p: o["checks"][0].update(status="fail", findings=[
            self.finding(p, quotes=[], original=[p["original_memory"]])]))
        self.assertEqual(result.status, "repair_not_supported")
        self.assertIn("knowledge_preservation", result.repair["validation"]["failed_checks"])

    def test_witnesses_need_not_be_edit_support_but_must_be_independently_cited(self):
        def mutate(o, p):
            o["edit_checks"][0]["citations"] = o["edit_checks"][0]["citations"][:1]
        result, _ = self.run_guard(mutate)
        self.assertEqual(result.commit_decision, "commit_repaired")
        self.assertTrue(result.repair["validation"]["witnesses_ok"])

    def test_witness_without_own_quote_policy_location_or_unique_id_cannot_pass(self):
        mutations = [lambda o, p: o["witness_checks"][0].update(citations=[]),
                     lambda o, p: o["witness_checks"][0].update(policy_quotes=[]),
                     lambda o, p: o["witness_checks"][0].update(policy_quotes=["A fabricated policy span."]),
                     lambda o, p: o["witness_checks"][0].update(record_id=o["witness_checks"][1]["record_id"]),
                     lambda o, p: o["witness_checks"][0].update(status="uncertainty_preserved")]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                result, _ = self.run_guard(mutate)
                self.assertEqual(result.status, "repair_audit_inconclusive")

    def test_uncertainty_witness_can_be_located_outside_edit_support(self):
        quote = "Evidence conflicts; the applicable action remains uncertain."
        def repair(o, p):
            o["edits"][0]["new"] += " " + quote
        def audit(o, p):
            o["edit_checks"][0]["citations"] = o["edit_checks"][0]["citations"][:1]
            o["uncertainty_quotes"] = [quote]
            o["witness_checks"][1].update(status="uncertainty_preserved", policy_quotes=[quote])
        result, _ = self.run_guard(audit, repair)
        self.assertEqual(result.commit_decision, "commit_repaired")

    def test_invented_citation_mixed_with_valid_citations_is_rejected(self):
        fabricated = {"record_id": "record_001", "quote": "An invented observation that did not happen."}
        for stage in ("repair", "repair_check"):
            def mutate(o, p):
                o["edits" if stage == "repair" else "edit_checks"][0]["citations"].append(fabricated)
            result, models = self.run_guard(repair_mutate=mutate) if stage == "repair" else self.run_guard(mutate)
            self.assertEqual(result.commit_decision, "defer")
            if stage == "repair":
                self.assertFalse(models["repair_check"].prompts)

    def test_remove_with_residual_and_replace_with_empty_are_structurally_valid(self):
        for label in ("remove", "replace", "restrict", "expand", "observation"):
            result, _ = self.run_guard(repair_mutate=lambda o, p: o["edits"][0].update(effect=label))
            self.assertEqual(result.commit_decision, "commit_repaired", label)
        t = transition()
        original = "Preserve this supported rule. " + t.proposed_memory
        edits = [{"old": t.proposed_memory, "new": "", "effect": "replace",
                  "citations": [{"record_id": t.candidate_experiences[0].record_id,
                                 "quote": t.candidate_experiences[0].text}]}]
        self.assertEqual(TransitionGuard._apply_edits(original, edits, t.candidate_experiences),
                         "Preserve this supported rule.")

    def test_actual_f0223_remove_patch_applies_without_changing_label_or_text(self):
        # Read-only structural regression; this does NOT claim semantic approval.
        path = ROOT / "results/transition_guard_p4/20261116T134841_132267Z_live/case_results.jsonl"
        if not path.exists():
            self.skipTest("Historical local artifact is optional")
        row = next(r for r in read_jsonl(path) if r["experiment_group"] == "B4" and r["case_id"].startswith("F0223_"))
        review = row["transition_reviews"]["poisoned"]
        from memform.defenses.transition_guard import Evidence
        # Adapter reconstructs the exact runtime handles, without sending labels to a model.
        from memform.defenses.transition_guard.adapter import p4_transition
        case = next(c for c in read_jsonl(ROOT / "outputs/mcq_item_audit/accepted_cases.jsonl") if c["case_id"] == row["case_id"])
        ids = case["memory_building"]["poisoned_condition"]["order"]
        t, _ = p4_transition(case, ids, review["original_memory"], None, 120)
        edits = review["repair"]["generation"]["parsed"]["edits"]
        evidence = t.selected_experiences + tuple(Evidence(**e) for e in review["witnesses"])
        memory = TransitionGuard._apply_edits(t.proposed_memory, edits, evidence)
        self.assertIsNotNone(memory)
        self.assertEqual(len(memory.split()), 73)
        self.assertTrue(any(e["effect"] == "remove" and e["new"].strip() for e in edits))

    def test_b1_audits_no_witnesses_and_keeps_same_commit_requirements(self):
        result, models = self.run_guard(group="B1")
        self.assertEqual(result.commit_decision, "commit_repaired")
        self.assertEqual(models["repair_check"].prompts[0][1]["required_witness_ids"], [])

    def test_malformed_containers_fail_closed_without_runtime_exception(self):
        result, _ = self.run_guard()
        report = result.repair["support_check"]["parsed"]
        edits = result.repair["generation"]["parsed"]["edits"]
        t = transition()
        for key in ("checks", "edit_checks", "witness_checks", "evidence_conflicts", "uncertainty_quotes"):
            for value in (None, True, "wrong type", [None], [{}], [[1, 2]]):
                with self.subTest(key=key, value=value):
                    obj = copy.deepcopy(report)
                    obj[key] = value
                    validation = compile_audit(obj, t, result.final_memory, edits, t.omitted_experiences)
                    self.assertEqual(validation["effective_verdict"], "inconclusive")

    def test_summary_separates_protocol_uncertainty_from_semantic_failure(self):
        unknown, _ = self.run_guard(lambda o, p: o["checks"].pop())
        failed, _ = self.run_guard(lambda o, p: o["checks"][1].update(status="fail", findings=[self.finding(p)]))
        rows = [{"experiment_group": "B4", "case_id": "synthetic", "task_results": [],
                 "transition_reviews": {"clean": unknown.to_dict(), "poisoned": failed.to_dict()}}]
        counts = transition_statistics(rows, {})["totals"]
        self.assertEqual(counts["repair_audit_inconclusive"], 1)
        self.assertEqual(counts["repair_audit_unsupported"], 1)
        self.assertEqual(counts["repair_audit_protocol_failures"], 1)


if __name__ == "__main__":
    unittest.main()
