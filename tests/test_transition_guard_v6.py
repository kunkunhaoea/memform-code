"""Compact audit regressions: real evidence obligations survive serialization."""
from __future__ import annotations

import copy
import json
import unittest

from test_transition_guard import reviewers, transition
import test_transition_guard_v4 as audit_v4
from memform.defenses.transition_guard import TransitionGuard
from memform.defenses.transition_guard.audit import AUDIT_SCHEMA, CHECKS, compile_audit, expand_audit


def compact_report(expanded):
    """Lossless evidence references; remove only explanations optional in v3."""
    report = copy.deepcopy(expanded)
    table = []
    indices = {}
    policy = [unit["policy_quote"] for unit in report["policy_units"]]

    def citation(row):
        key = (row["record_id"], row["quote"])
        if key not in indices:
            indices[key] = len(table)
            table.append({"record_id": key[0], "quote": key[1]})
        return indices[key]

    def visit(value):
        if isinstance(value, list):
            for row in value:
                visit(row)
        elif isinstance(value, dict):
            status = value.get("status")
            if status in {"supported", "preserved", "qualified", "consistent", "uncertainty_preserved"}:
                value.pop("reason", None)
            for key, item in list(value.items()):
                if key == "citations":
                    value[key] = [citation(row) for row in item]
                elif key == "evidence_checks":
                    value[key] = [{"citation": citation(row), "relation": row["relation"],
                                   **({"reason": row["reason"]} if row["relation"] in {"contradicts", "uncertain"} else {})}
                                  for row in item]
                elif key in {"policy_quotes", "uncertainty_quotes"}:
                    value[key] = [policy.index(q) if q in policy else q for q in item]
                else:
                    visit(item)

    visit(report)
    report["checks"] = [[row["check"], row["status"] if row["status"] in {"pass", "unknown"}
                         else {"status": row["status"], "findings": row["findings"]}]
                        for row in report["checks"]]
    report.update(report_format="compact-v1", citation_table=table)
    report.pop("reason", None)
    return report


class CompactAuditTests(unittest.TestCase):
    def setUp(self):
        self.result, self.models = audit_v4.AuditV4Tests().run_guard()
        self.t = transition()
        self.memory = self.result.final_memory
        self.edits = self.result.repair["generation"]["parsed"]["edits"]
        self.full = self.result.repair["support_check"]["parsed"]
        self.compact = compact_report(self.full)

    def compile(self, report=None):
        return compile_audit(self.compact if report is None else report, self.t, self.memory,
                             self.edits, self.t.omitted_experiences)

    def test_compact_and_expanded_audits_have_identical_decisions_and_evidence(self):
        expanded = self.compile(self.full)
        compact = self.compile()
        self.assertEqual(compact, expanded)
        self.assertEqual(compact["effective_verdict"], "supported")
        self.assertTrue(compact["selected_evidence_ok"])
        self.assertTrue(compact["policy_units_ok"])
        self.assertEqual(compact["permission_units"], 1)
        self.assertLess(len(json.dumps(self.compact)), len(json.dumps(self.full)) * .8)

    def test_reason_free_selection_preserves_valid_witnesses(self):
        models = reviewers("no_gap")
        models["selection"].invoke = lambda system, user: json.dumps({"witnesses": [
            {"record_id": row["record_id"], "quote": row["text"]}
            for row in json.loads(user)["omitted"][:2]]})
        result = TransitionGuard("B4", models).review(self.t, lambda _: "A restricted policy.")
        self.assertEqual([r["record_id"] for r in result.witnesses],
                         [e.record_id for e in self.t.omitted_experiences])
        self.assertEqual(result.counterfactual_rebuilds, 2)
        captured = reviewers("no_gap")
        TransitionGuard("B4", captured).review(self.t, lambda _: "A restricted policy.")
        prompt = captured["selection"].prompts[0][0]
        self.assertNotIn('"reason"', prompt)
        self.assertIn("do not generate selection reasons", prompt)

    def test_guard_accepts_compact_audit_in_a_fresh_independent_call(self):
        models = reviewers()
        original = models["repair_check"].invoke
        models["repair_check"].invoke = lambda system, user: json.dumps(compact_report(json.loads(original(system, user))))
        result = TransitionGuard("B4", models).review(self.t, lambda _: "A restricted policy.")
        self.assertEqual(result.commit_decision, "commit_repaired")
        self.assertEqual(len(models["repair_check"].prompts), 1)
        system, payload = models["repair_check"].prompts[0]
        self.assertIn("citation_table", system)
        self.assertIn("TWO DISTINCT", system)
        self.assertNotIn("confirmed_defects", payload)
        self.assertNotIn("reason", payload["edits"][0])
        self.assertEqual(set(payload["required_checks"]), set(CHECKS))
        # Logs preserve the compact raw/parsed response, not invented explanations.
        self.assertEqual(result.repair["support_check"]["parsed"]["report_format"], "compact-v1")
        self.assertNotIn("reason", result.repair["support_check"]["parsed"]["policy_units"][0])

    def test_references_reject_negative_boolean_out_of_range_and_wrong_types(self):
        for invalid in (-1, True, len(self.compact["citation_table"]), "0", None, {}):
            for field in ("citation", "policy"):
                with self.subTest(invalid=invalid, field=field):
                    report = copy.deepcopy(self.compact)
                    row = report["selected_evidence_review"]
                    row["citations" if field == "citation" else "policy_quotes"] = [invalid]
                    result = self.compile(report)
                    self.assertEqual(result["effective_verdict"], "inconclusive")
                    self.assertTrue(result["protocol_errors"])

    def test_missing_table_or_format_cannot_be_approved(self):
        for key in ("citation_table", "report_format"):
            report = copy.deepcopy(self.compact)
            report.pop(key)
            self.assertEqual(self.compile(report)["effective_verdict"], "inconclusive")

    def test_malformed_compact_containers_fail_closed(self):
        for key in ("citation_table", "checks", "policy_units", "witness_checks", "edit_checks"):
            for value in (None, True, "wrong", [], [None], [{}], [[1]]):
                with self.subTest(key=key, value=value):
                    report = copy.deepcopy(self.compact)
                    report[key] = value
                    self.assertNotEqual(self.compile(report)["effective_verdict"], "supported")
        for value in (None, True, "wrong", [], [None], [{}], [[1]]):
            report = copy.deepcopy(self.compact)
            report["policy_units"][0]["evidence_checks"] = value
            self.assertNotEqual(self.compile(report)["effective_verdict"], "supported")

    def test_compact_table_capitalization_corrections_preserve_raw_reply(self):
        models = reviewers()
        raw = json.dumps({"citation_table": [{"record_id": "e", "quote": "the verified record was retained"}]})
        models["repair_check"].invoke = lambda *_: raw
        reply = TransitionGuard("B4", models)._ask("repair_check", "Return JSON.", {
            "evidence": [{"record_id": "e", "text": "Observed outcome: The verified record was retained."}]})
        self.assertEqual(reply["raw_output"], raw)
        self.assertEqual(reply["parsed"]["citation_table"][0]["quote"], "The verified record was retained")
        self.assertEqual(len(reply["citation_corrections"]), 1)

    def test_every_table_quote_is_validated_even_if_unused(self):
        for citation in ({"record_id": "record_003", "quote": "A fabricated observation that never occurred."},
                         {"record_id": "unknown", "quote": self.t.selected_experiences[0].text}, None):
            report = copy.deepcopy(self.compact)
            report["citation_table"].append(citation)
            result = self.compile(report)
            self.assertEqual(result["effective_verdict"], "inconclusive")
            self.assertTrue(any("invalid_citation" in e for e in result["protocol_errors"]))

    def test_duplicate_sources_do_not_satisfy_two_record_branch_support(self):
        report = copy.deepcopy(self.compact)
        checks = report["policy_units"][0]["evidence_checks"]
        # Different table positions still refer to the same observation.
        source = checks[0]["citation"]
        report["citation_table"].append(copy.deepcopy(report["citation_table"][source]))
        checks[1]["citation"] = len(report["citation_table"]) - 1
        result = self.compile(report)
        self.assertNotEqual(result["effective_verdict"], "supported")

    def test_other_actions_and_uncertain_evidence_do_not_count_as_positive_support(self):
        for relation in ("context_only", "contradicts", "uncertain"):
            report = copy.deepcopy(self.compact)
            assessment = report["policy_units"][0]["evidence_checks"][1]
            assessment.update(relation=relation, reason="This observation does not support that action under these conditions.")
            result = self.compile(report)
            self.assertEqual(result["effective_verdict"], "inconclusive")
            self.assertIn("policy_unit:0:insufficient_branch_support", result["unknown_checks"])

    def test_incomplete_policy_coverage_is_still_rejected(self):
        report = copy.deepcopy(self.compact)
        report["policy_units"][0]["policy_quote"] = self.memory[:-10]
        self.assertIn("policy_units:incomplete_coverage", self.compile(report)["protocol_errors"])

    def test_missing_selected_review_or_witness_is_still_rejected(self):
        report = copy.deepcopy(self.compact)
        report.pop("selected_evidence_review")
        self.assertNotEqual(self.compile(report)["effective_verdict"], "supported")
        report = copy.deepcopy(self.compact)
        report["witness_checks"].pop()
        self.assertNotEqual(self.compile(report)["effective_verdict"], "supported")

    def test_checks_require_all_six_and_unknown_does_not_pass(self):
        for mutation in (lambda r: r["checks"].pop(2),
                         lambda r: r["checks"].append(copy.deepcopy(r["checks"][0])),
                         lambda r: r["checks"][2].__setitem__(1, "unknown"),
                         lambda r: r["checks"][2].__setitem__(1, "fail"),
                         lambda r: r["checks"][2].__setitem__(1, {"status": "fail", "findings": []})):
            report = copy.deepcopy(self.compact)
            mutation(report)
            self.assertNotEqual(self.compile(report)["effective_verdict"], "supported")

    def test_failure_findings_keep_exact_original_and_repaired_locations(self):
        report = copy.deepcopy(self.compact)
        finding = {"original_quotes": [], "policy_quotes": [0], "citations": [0],
                   "reason": "This procedure lacks support."}
        report["checks"][1][1] = {"status": "fail", "findings": [finding]}
        result = self.compile(report)
        self.assertEqual(result["effective_verdict"], "unsupported")
        self.assertFalse(result["protocol_errors"])
        finding["policy_quotes"] = ["This policy span never occurred."]
        self.assertEqual(self.compile(report)["effective_verdict"], "inconclusive")
        finding["policy_quotes"] = [0]
        finding["original_quotes"] = [0]  # original locations are never unit indices
        self.assertEqual(self.compile(report)["effective_verdict"], "inconclusive")

    def test_bad_or_uncertain_assessments_still_require_explanation(self):
        for relation in ("contradicts", "uncertain"):
            report = copy.deepcopy(self.compact)
            report["policy_units"][0]["evidence_checks"][1]["relation"] = relation
            self.assertTrue(self.compile(report)["protocol_errors"])
        for section, status in (("selected_evidence_review", "inconclusive"),
                                ("policy_units", "unsupported"), ("edit_checks", "inconclusive"),
                                ("witness_checks", "conflict")):
            report = copy.deepcopy(self.compact)
            row = report[section] if isinstance(report[section], dict) else report[section][0]
            row["status"] = status
            self.assertTrue(self.compile(report)["protocol_errors"])

    def test_global_verdict_or_old_schema_cannot_override_compact_checks(self):
        for fields in ({"verdict": "supported", "no_invented_facts": False},
                       {"schema_version": "repair-audit-v2"}):
            report = copy.deepcopy(self.compact)
            report.update(fields)
            self.assertEqual(self.compile(report)["effective_verdict"], "inconclusive")

    def test_uncertainty_unit_references_require_real_uncertainty_and_own_witness(self):
        report = copy.deepcopy(self.compact)
        report["uncertainty_quotes"] = [0]
        report["witness_checks"][0]["status"] = "uncertainty_preserved"
        expanded, errors = expand_audit(report, self.t.selected_experiences + self.t.omitted_experiences)
        self.assertFalse(errors)
        self.assertEqual(expanded["uncertainty_quotes"], [self.memory])
        self.assertEqual(self.compile(report)["effective_verdict"], "supported")
        report["witness_checks"][0]["citations"] = report["selected_evidence_review"]["citations"]
        self.assertNotEqual(self.compile(report)["effective_verdict"], "supported")


if __name__ == "__main__":
    unittest.main()
