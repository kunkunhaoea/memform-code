"""Offline checks of reusable history composition, pairing and source integrity."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from memform.common import read_jsonl, write_json, write_jsonl
from memform.defenses.transition_guard.adapter import p4_transition
from memform.eval.core import build_batch_memory
from experiments.transition_guard_p4.expand_history import (
    DEFAULT_SOURCE, build_bundle, donor_bank, exclusion_reason, expanded_case,
    family_signatures, file_hash, make_plan, verify_bundle,
)


class CaptureModel:
    def __init__(self):
        self.calls = []

    def invoke(self, system, user):
        self.calls.append((system, user))
        return "A synthetic policy for protocol testing only."


class HistoryExpansionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = read_jsonl(DEFAULT_SOURCE)
        cls.p1 = [c for c in source if c["interface"] == "P1"][:30]
        cls.p4 = [c for c in source if c["interface"] == "P4"][:2]
        cls.rows = cls.p1 + cls.p4
        cls.bank = donor_bank(cls.rows)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.jsonl"
        write_jsonl(self.source, self.rows)
        self.output = self.root / "bundle"

    def build(self):
        return build_bundle(self.source, self.output, [9, 12, 20], 42)

    def test_exact_nesting_preserves_tasks_core_selected_order_and_full_pool(self):
        digest = file_hash(self.source)
        self.build()
        previous = {}
        for n in (9, 12, 20):
            rows = read_jsonl(self.output / f"N{n}/cases.jsonl")
            for case in rows:
                original = next(c for c in self.p4 if c["case_id"] == case["case_id"])
                self.assertEqual(len(case["candidate_experiences"]), n)
                self.assertEqual(case["candidate_experiences"][:9], original["candidate_experiences"])
                self.assertEqual(case["evaluation"], original["evaluation"])
                self.assertEqual(case["clean_policy"], original["clean_policy"])
                self.assertEqual(case["poisoned_policy"], original["poisoned_policy"])
                self.assertEqual(case["memory_building"]["batch_prompt"], original["memory_building"]["batch_prompt"])
                for side in ("clean", "poisoned"):
                    self.assertEqual(case["memory_building"][side + "_condition"],
                                     original["memory_building"][side + "_condition"])
                records = case["candidate_experiences"]
                self.assertEqual(len({e["id"] for e in records}), n)
                self.assertEqual(len({e["text"] for e in records}), n)
                order = case["memory_building"]["mechanism_controls"]["full_pool"]["experience_ids"]
                self.assertEqual(set(order), {e["id"] for e in records})
                core_ids = {e["id"] for e in original["candidate_experiences"]}
                self.assertEqual([eid for eid in order if eid in core_ids],
                                 original["memory_building"]["mechanism_controls"]["full_pool"]["experience_ids"])
                if n == 9:
                    self.assertEqual(case, original)
                else:
                    small = previous[case["case_id"]]
                    self.assertEqual(records[:len(small["candidate_experiences"])], small["candidate_experiences"])
                    small_ids = {e["id"] for e in small["candidate_experiences"]}
                    self.assertEqual([eid for eid in order if eid in small_ids],
                                     small["memory_building"]["mechanism_controls"]["full_pool"]["experience_ids"])
                previous[case["case_id"]] = case
        self.assertEqual(file_hash(self.source), digest)

    def test_donors_are_unmodified_other_workflows_and_do_not_import_benchmark_metadata(self):
        case = self.p4[0]
        plan = make_plan(case, self.bank, 100, 42)
        indexed = {d["bank_id"]: d for d in self.bank}
        signatures = family_signatures(self.bank)
        result = expanded_case(case, plan, indexed, 109, 42)
        for record, bid in zip(result["candidate_experiences"][9:], plan["donor_bank_ids"]):
            donor = indexed[bid]
            self.assertEqual(set(record), {"id", "text"})
            self.assertEqual(record["text"], donor["text"])
            self.assertIsNone(exclusion_reason(case, donor["source_family_id"],
                                              signatures[donor["source_family_id"]]))
        self.assertNotIn(case["family_id"], {indexed[bid]["source_family_id"] for bid in plan["donor_bank_ids"]})

    def test_generation_does_not_read_gold_policies_answers_or_model_outcomes(self):
        case = copy.deepcopy(self.p4[0])
        expected = make_plan(case, self.bank, 30, 42)
        case.update(evaluation={"do_not_read": "answers"}, clean_policy="not available", poisoned_policy="not available")
        # Full-case hashes deliberately change; selected donor IDs must not.
        actual = make_plan(case, self.bank, 30, 42)
        self.assertEqual(actual["donor_bank_ids"], expected["donor_bank_ids"])
        changed = copy.deepcopy(self.rows)
        for c in changed:
            if c["interface"] == "P1":
                c["memory_building"] = {"do_not_read": "injected memory"}
                c["evaluation"] = {"do_not_read": "answers"}
                for e in c["candidate_experiences"]:
                    e.update(supports="not available", success="not available")
        self.assertEqual(donor_bank(changed), self.bank)

    def test_deterministic_plans_and_sizes_do_not_depend_on_source_order_or_max_n(self):
        self.assertEqual(donor_bank(list(reversed(self.rows))), self.bank)
        for c in self.p4:
            short = make_plan(c, self.bank, 25, 42)
            long = make_plan(c, self.bank, 100, 42)
            self.assertEqual(short["donor_bank_ids"], long["donor_bank_ids"][:25])
            self.assertNotEqual(short["donor_bank_ids"], make_plan(c, self.bank, 25, 43)["donor_bank_ids"])

    def test_original_updater_prompts_and_full_pool_replay_remain_faithful(self):
        c = self.p4[0]
        plan = make_plan(c, self.bank, 41, 42)
        enlarged = expanded_case(c, plan, {d["bank_id"]: d for d in self.bank}, 50, 42)
        for side in ("clean", "poisoned"):
            original, expanded = CaptureModel(), CaptureModel()
            ids = c["memory_building"][side + "_condition"]["order"]
            for model, case in ((original, c), (expanded, enlarged)):
                build_batch_memory(model, case, ids, case["memory_building"]["batch_prompt"], 120)
            self.assertEqual(original.calls, expanded.calls)
        model = CaptureModel()
        t, updater = p4_transition(enlarged, ids, "candidate", model, 120)
        updater(t.candidate_experiences)
        prompt = model.calls[0][1]
        for e in enlarged["candidate_experiences"]:
            self.assertIn(f"[{e['id']}] {e['text']}", prompt)
        self.assertNotIn("source_family_id", prompt)
        self.assertNotIn("background_policy", prompt)

    def test_reusing_bundle_does_not_rewrite_files_and_new_configuration_is_rejected(self):
        self.build()
        times = {p: p.stat().st_mtime_ns for p in self.output.rglob("*") if p.is_file()}
        result = self.build()
        self.assertEqual(result["status"], "reused_without_rewriting")
        self.assertEqual(times, {p: p.stat().st_mtime_ns for p in times})
        with self.assertRaisesRegex(ValueError, "configuration differs"):
            build_bundle(self.source, self.output, [9, 12, 20], 43)

    def test_corruption_is_rejected_even_if_file_hash_is_updated(self):
        self.build()
        path = self.output / "N20/cases.jsonl"
        rows = read_jsonl(path)
        rows[0]["evaluation"]["trigger_tasks"][0]["clean_answer"] = "FORGED"
        write_jsonl(path, rows)
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            verify_bundle(self.output, self.source)
        manifest_path = self.output / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["files"]["N20/cases.jsonl"]["sha256"] = file_hash(path)
        write_json(manifest_path, manifest)
        with self.assertRaisesRegex(ValueError, "task, core, background or ordering changed"):
            verify_bundle(self.output, self.source)

    def test_insufficient_unique_donors_and_incomplete_destination_do_not_get_overwritten(self):
        with self.assertRaisesRegex(ValueError, "only .* pass screening"):
            build_bundle(self.source, self.output, [10000], 42)
        self.assertFalse(self.output.exists())
        self.output.mkdir()
        sentinel = self.output / "keep.txt"
        sentinel.write_text("existing user data")
        with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
            self.build()
        self.assertEqual(sentinel.read_text(), "existing user data")

    def test_manifests_match_data_hash_and_use_same_family_order_at_every_n(self):
        self.build()
        reference = None
        for n in (9, 12, 20):
            folder = self.output / f"N{n}"
            manifest = json.loads((folder / "selected_cases.json").read_text())
            self.assertEqual(manifest["source_sha256"], file_hash(folder / "cases.jsonl"))
            self.assertEqual(manifest["seed"], 42)
            if reference is None:
                reference = manifest["case_ids"]
            self.assertEqual(manifest["case_ids"], reference)


if __name__ == "__main__":
    unittest.main()
