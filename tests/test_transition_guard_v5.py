"""Offline regressions for quotation handling and branch-level audit obligations."""
from __future__ import annotations

import copy
import json
import unittest

from test_transition_guard import ROOT, reviewers, transition
import test_transition_guard_v4 as audit_v4
from memform.common import read_jsonl
from memform.defenses.transition_guard import Evidence, TransitionGuard
from memform.defenses.transition_guard.adapter import p4_transition
from memform.defenses.transition_guard.audit import AUDIT_SCHEMA, compile_audit
from memform.defenses.transition_guard.citations import observation_quote
from experiments.transition_guard_p4.analyze_results import transition_statistics


class AuditV5Tests(unittest.TestCase):
    def setUp(self):
        self.result, self.models = audit_v4.AuditV4Tests().run_guard()
        self.report = self.result.repair["support_check"]["parsed"]
        self.edits = self.result.repair["generation"]["parsed"]["edits"]
        self.t = transition()

    def compile(self, report=None):
        return compile_audit(self.report if report is None else report, self.t, self.result.final_memory,
                             self.edits, self.t.omitted_experiences)

    def test_selected_review_is_explicit_and_separate_from_edit_support(self):
        system, payload = self.models["repair_check"].prompts[0]
        self.assertIn("selected_evidence_review", system)
        self.assertEqual(payload["selected_record_ids"], [e.record_id for e in self.t.selected_experiences])
        report = copy.deepcopy(self.report)
        witness_cites = [{"record_id": e.record_id, "quote": e.text} for e in self.t.omitted_experiences]
        report["edit_checks"][0]["citations"] = witness_cites
        report["policy_units"][0]["evidence_checks"] = [
            {**c, "relation": "supports", "reason": "Branch supported in this synthetic report."} for c in witness_cites]
        self.assertEqual(self.compile(report)["effective_verdict"], "supported")
        report.pop("selected_evidence_review")
        self.assertEqual(self.compile(report)["effective_verdict"], "inconclusive")

    def test_selected_review_cannot_use_omitted_or_fabricated_evidence(self):
        for cites in ([], [{"record_id": "record_003", "quote": self.t.omitted_experiences[0].text}],
                      [{"record_id": "record_001", "quote": "An invented observation quote."}]):
            report = copy.deepcopy(self.report)
            report["selected_evidence_review"]["citations"] = cites
            self.assertIn("selected_evidence_review:invalid_or_unlocated", self.compile(report)["protocol_errors"])

    def test_selected_review_semantic_failure_is_not_a_format_failure(self):
        report = copy.deepcopy(self.report)
        report["selected_evidence_review"]["status"] = "unsupported"
        result = self.compile(report)
        self.assertEqual(result["effective_verdict"], "unsupported")
        self.assertFalse(result["protocol_errors"])

    def test_all_policy_text_including_unchanged_fallback_must_be_covered(self):
        report = copy.deepcopy(self.report)
        report["policy_units"][0]["policy_quote"] = "Merge only when authenticated account and order match;"
        result = self.compile(report)
        self.assertIn("policy_units:incomplete_coverage", result["protocol_errors"])
        report["policy_units"].append({
            **copy.deepcopy(self.report["policy_units"][0]), "policy_quote": "otherwise keep separate."})
        self.assertEqual(self.compile(report)["effective_verdict"], "supported")
        report["policy_units"].reverse()
        self.assertEqual(self.compile(report)["effective_verdict"], "inconclusive")

    def test_other_action_records_do_not_count_as_branch_support(self):
        report = copy.deepcopy(self.report)
        unit = report["policy_units"][0]
        unit["introduces_permission"] = True
        unit["evidence_checks"][1]["relation"] = "context_only"
        result = self.compile(report)
        self.assertEqual(result["effective_verdict"], "inconclusive")
        self.assertIn("policy_unit:0:insufficient_branch_support", result["unknown_checks"])
        self.assertFalse(result["protocol_errors"])

    def test_original_single_success_fallback_is_not_exempt_from_support_gate(self):
        report = copy.deepcopy(self.report)
        report["policy_units"][0]["introduces_permission"] = False
        report["policy_units"][0]["evidence_checks"] = report["policy_units"][0]["evidence_checks"][:1]
        self.assertEqual(self.compile(report)["effective_verdict"], "inconclusive")

    def test_unsupported_branch_cannot_be_overridden_by_six_global_passes(self):
        report = copy.deepcopy(self.report)
        report["policy_units"][0]["status"] = "unsupported"
        result = self.compile(report)
        self.assertEqual(result["effective_verdict"], "unsupported")
        self.assertFalse(result["protocol_errors"])

    def test_two_supporting_records_cannot_override_counterevidence(self):
        for relation in ("contradicts", "uncertain"):
            report = copy.deepcopy(self.report)
            record = self.t.omitted_experiences[0]
            report["policy_units"][0]["evidence_checks"].append({
                "record_id": record.record_id, "quote": record.text, "relation": relation,
                "reason": "This relevant record does not support the branch."})
            self.assertEqual(self.compile(report)["effective_verdict"], "inconclusive")

    def test_duplicate_record_is_not_two_supporting_observations(self):
        report = copy.deepcopy(self.report)
        items = report["policy_units"][0]["evidence_checks"]
        items[1] = copy.deepcopy(items[0])
        self.assertIn("policy_unit:0:invalid_evidence_assessment", self.compile(report)["protocol_errors"])

    def test_nested_exact_uncertainty_spans_need_not_be_identical_strings(self):
        sentence = "Evidence conflicts; the applicable action remains uncertain."
        def repair(obj, payload):
            obj["edits"][0]["new"] += " " + sentence
        def audit(obj, payload):
            obj["uncertainty_quotes"] = [sentence]
            obj["witness_checks"][1].update(status="uncertainty_preserved",
                                             policy_quotes=["the applicable action remains uncertain."])
        result, _ = audit_v4.AuditV4Tests().run_guard(audit, repair)
        self.assertEqual(result.commit_decision, "commit_repaired")
        def unrelated(obj, payload):
            audit(obj, payload)
            obj["witness_checks"][1]["policy_quotes"] = ["Merge only when authenticated account and order match"]
        result, _ = audit_v4.AuditV4Tests().run_guard(unrelated, repair)
        self.assertEqual(result.commit_decision, "defer")

    def test_new_containers_and_legacy_schema_fail_closed(self):
        for key in ("selected_evidence_review", "policy_units"):
            for value in (None, True, "wrong", [], [None], [{}], [[1]]):
                with self.subTest(key=key, value=value):
                    report = copy.deepcopy(self.report)
                    report[key] = value
                    self.assertEqual(self.compile(report)["effective_verdict"], "inconclusive")
        report = copy.deepcopy(self.report)
        report["schema_version"] = "repair-audit-v1"
        self.assertIn("schema_version", self.compile(report)["protocol_errors"])
        self.assertEqual(AUDIT_SCHEMA, "repair-audit-v3")

    def test_new_counters_distinguish_quote_correction_and_branch_failure(self):
        report = copy.deepcopy(self.report)
        report["policy_units"][0]["status"] = "unsupported"
        review = self.result.to_dict()
        review["selection"]["citation_corrections"] = [{"path": "synthetic"}]
        review["repair"]["validation"] = self.compile(report)
        rows = [{"experiment_group": "B4", "case_id": "synthetic", "task_results": [],
                 "transition_reviews": {"clean": review}}]
        counts = transition_statistics(rows, {})["totals"]
        self.assertEqual(counts["citation_capitalization_corrections"], 1)
        self.assertEqual(counts["repair_branch_support_failures"], 1)


class QuoteV5Tests(unittest.TestCase):
    def test_unique_sentence_initial_capitalization_is_canonicalized(self):
        text = "Observed outcome: The cache retained its verified object."
        self.assertEqual(observation_quote(text, "the cache retained its verified object"),
                         "The cache retained its verified object")
        self.assertEqual(observation_quote(text, "The cache retained its verified object"),
                         "The cache retained its verified object")

    def test_identifiers_negation_whitespace_and_interior_case_are_not_normalized(self):
        for text, quote in (
            ("Context: API identifiers must match exactly.", "api identifiers must match exactly"),
            ("Context: The account ID was ABC-123.", "the account ID was abc-123"),
            ("Context: The record was not verified.", "the record was verified"),
            ("Context: The record was not verified.", "the  record was not verified"),
            ("Context: kept The verified record intact.", "the verified record intact"),
            ("The record was verified. The record was verified.", "the record was verified"),
        ):
            with self.subTest(quote=quote):
                self.assertIsNone(observation_quote(text, quote))

    def test_parsed_corrections_are_logged_without_changing_raw_reply(self):
        models = reviewers()
        raw = json.dumps({"witnesses": [{"record_id": "e", "quote": "the verified record was retained"}]})
        models["selection"].invoke = lambda *_: raw
        reply = TransitionGuard("B4", models)._ask("selection", "Return JSON.", {
            "omitted": [{"record_id": "e", "text": "Observed outcome: The verified record was retained."}]})
        self.assertEqual(reply["raw_output"], raw)
        self.assertEqual(reply["parsed"]["witnesses"][0]["quote"], "The verified record was retained")
        self.assertEqual(len(reply["citation_corrections"]), 1)

    def test_edit_old_and_policy_locations_remain_strictly_exact(self):
        t = transition()
        edit = {"old": t.proposed_memory.lower(), "new": "A restricted policy.", "effect": "restrict",
                "citations": [{"record_id": t.selected_experiences[0].record_id,
                               "quote": t.selected_experiences[0].text}]}
        self.assertIsNone(TransitionGuard._apply_edits(t.proposed_memory, [edit], t.selected_experiences))

    def test_actual_f0104_and_f0152_quotes_are_locatable_without_api(self):
        path = ROOT / "results/transition_guard_p4/20261116T151049_721096Z_live/call_events.jsonl"
        if not path.exists():
            self.skipTest("Historical local artifact is optional")
        events = read_jsonl(path)
        hits = set()
        for event in events:
            if event["group"] != "B4" or event["role"] not in ("selection", "repair"):
                continue
            payload = json.loads(event["user"])
            if event["case_id"].startswith("F0104_") and event["role"] == "selection" and "promptly evict" in payload["proposed_memory"]:
                evidence = tuple(Evidence(**e) for e in payload["omitted"])
                reply = json.loads(event["raw_output"])
                pool = {e.record_id: e.text for e in evidence}
                self.assertTrue(all(observation_quote(pool[w["record_id"]], w["quote"]) for w in reply["witnesses"]))
                hits.add("F0104")
            if event["case_id"].startswith("F0152_") and event["role"] == "repair" and "their absence did not produce" in payload["original_memory"]:
                edits = json.loads(event["raw_output"])["edits"]
                evidence = tuple(Evidence(**e) for e in payload["evidence"])
                self.assertIsNotNone(TransitionGuard._apply_edits(payload["original_memory"], edits, evidence))
                hits.add("F0152")
        self.assertEqual(hits, {"F0104", "F0152"})

    def test_actual_f0068_cannot_pool_cancellation_and_hold_support(self):
        path = ROOT / "results/transition_guard_p4/20261116T151049_721096Z_live/case_results.jsonl"
        if not path.exists():
            self.skipTest("Historical local artifact is optional")
        row = next(r for r in read_jsonl(path) if r["experiment_group"] == "B4" and r["case_id"].startswith("F0068_"))
        review = row["transition_reviews"]["poisoned"]
        case = next(c for c in read_jsonl(ROOT / "outputs/mcq_item_audit/accepted_cases.jsonl") if c["case_id"] == row["case_id"])
        t, _ = p4_transition(case, case["memory_building"]["poisoned_condition"]["order"],
                             review["original_memory"], None, 120)
        report = copy.deepcopy(review["repair"]["support_check"]["parsed"])
        report["schema_version"] = AUDIT_SCHEMA
        memory = review["final_memory"]
        evidence = t.selected_experiences + tuple(Evidence(**e) for e in review["witnesses"])
        by_id = {e.record_id: e.text for e in evidence}
        def assessment(rid, relation):
            return {"record_id": rid, "quote": by_id[rid], "relation": relation,
                    "reason": "Synthetic explicit relation for the recorded action branch."}
        branch = "or when no completed cancellation record is found and the requester has not responded."
        start = memory.index(branch)
        observation_start = memory.index("Evidence:")
        report["selected_evidence_review"] = {
            "status": "qualified", "citations": [{"record_id": t.selected_experiences[0].record_id,
                "quote": t.selected_experiences[0].text}], "policy_quotes": [memory], "reason": "Synthetic source review."}
        report["policy_units"] = [
            {"policy_quote": memory[:start].rstrip(), "kind": "permission", "status": "supported",
             "introduces_permission": False, "evidence_checks": [assessment("record_004", "supports"),
                 assessment("record_005", "context_only")], "reason": "Cancellation evidence is not no-response evidence."},
            {"policy_quote": branch, "kind": "permission", "status": "supported",
             "introduces_permission": False, "evidence_checks": [assessment("record_005", "supports"),
                 assessment("record_004", "context_only"), assessment("record_003", "context_only"),
                 assessment("record_006", "context_only")], "reason": "One no-response success does not establish a reusable permission."},
            {"policy_quote": memory[start + len(branch):observation_start].strip(), "kind": "permission",
             "status": "supported", "introduces_permission": False,
             "evidence_checks": [assessment("record_003", "supports"), assessment("record_006", "supports")],
             "reason": "Hold-and-contact branch has its own distinct supporting observations."},
            {"policy_quote": memory[observation_start:], "kind": "observation"}]
        result = compile_audit(report, t, memory, review["repair"]["generation"]["parsed"]["edits"],
                               tuple(Evidence(**e) for e in review["witnesses"]))
        self.assertFalse(result["protocol_errors"])
        self.assertEqual(result["effective_verdict"], "inconclusive")
        self.assertIn("policy_unit:1:insufficient_branch_support", result["unknown_checks"])
        # This exercises a constructed branch audit over real text, not a new
        # model judgment or evidence of downstream recovery.


if __name__ == "__main__":
    unittest.main()
