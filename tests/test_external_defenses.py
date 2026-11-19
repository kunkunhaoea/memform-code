"""Native hooks, fixed references, costs, blocking and safe resume (no API)."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from memform.common import read_jsonl
from experiments.transition_guard_p4.compare_external import ROOT, analyze, read_reference
from experiments.transition_guard_p4.external_defenses import completed


class ExternalDefenseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.temp.name)
        cls.cases = ROOT / "data/transition_guard_p4_large_history/v1_seed42/N1000/cases.jsonl"
        cls.manifest = cls.cases.parent / "selected_cases_32.json"
        cls.reference = cls.directory / "reference"
        cls.output = cls.directory / "comparison"
        cls.invoke("run_experiment", "--mode", "mock", "--cases", cls.cases,
                   "--manifest", cls.manifest, "--groups", "B0", "B2", "B4", "B5", "--limit", "2",
                   "--cache", cls.directory / "cache.jsonl", "--output-dir", cls.reference)
        cls.invoke("compare_external", *cls.arguments(), "--output-dir", cls.output)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @staticmethod
    def invoke(module, *args):
        result = subprocess.run([sys.executable, "-B", "-m", "experiments.transition_guard_p4." + module,
                                 *map(str, args)], cwd=ROOT, capture_output=True, text=True)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)
        return result

    @classmethod
    def arguments(cls):
        return ["--mode", "mock", "--cases", cls.cases, "--manifest", cls.manifest,
                "--reference-run", cls.reference, "--limit", "2"]

    def test_native_defenses_complete_without_api_or_rerunning_reference(self):
        s = json.loads((self.output / "summary.json").read_text())
        self.assertTrue(s["complete"])
        self.assertTrue(s["synthetic_results"])
        self.assertEqual(s["new_run_cost"]["api_attempts"], 0)
        self.assertEqual(set(s["groups"]), {"B0", "B2", "B4", "B5", "D1", "D2", "D3"})
        self.assertEqual([s["groups"][g]["rows"] for g in ("D1", "D2", "D3")], [2, 2, 2])
        self.assertTrue(all(s["groups"][g]["new_run_cost"]["logical_invocations"] == 0
                            for g in ("B0", "B2", "B4", "B5")))

    def test_reference_outcomes_and_event_ranges_preserved(self):
        original = {(r["experiment_group"], r["case_id"]): r for r in read_jsonl(self.reference / "case_results.jsonl")}
        events = read_jsonl(self.output / "call_events.jsonl")
        for row in read_jsonl(self.output / "case_results.jsonl"):
            if row["experiment_group"].startswith("B"):
                old = original[(row["experiment_group"], row["case_id"])]
                self.assertEqual(row["task_results"], old["task_results"])
                self.assertEqual(row["learned_memories"], old["learned_memories"])
            for event in events[slice(*row["call_event_range"])]:
                self.assertEqual((event["group"], event["case_id"]), (row["experiment_group"], row["case_id"]))

    def test_native_filters_only_see_retained_records(self):
        logs = read_jsonl(self.output / "defense_log.jsonl")
        for group in ("D1", "D3"):
            decisions = [r for r in logs if r["experiment_group"] == group]
            self.assertEqual(len(decisions), 16)  # 2 cases x 2 sides x S=4
            self.assertFalse(any(r["condition"].startswith("mechanism_control:") for r in decisions))
        native = read_jsonl(self.cases)[0]
        self.assertEqual(len(native["candidate_experiences"]), 1000)
        self.assertIn("full_pool", native["memory_building"]["mechanism_controls"])

    def test_resume_executes_no_completed_cells(self):
        out = self.directory / "resumed"
        self.invoke("compare_external", *self.arguments(), "--resume-from", self.output, "--output-dir", out)
        s = json.loads((out / "summary.json").read_text())
        self.assertTrue(s["complete"])
        self.assertEqual(s["new_run_cost"]["logical_invocations"], 0)
        self.assertEqual(len(read_jsonl(out / "case_results.jsonl")), 14)

    def test_semantic_block_is_outcome_but_parse_failure_is_not(self):
        row = copy.deepcopy(next(r for r in read_jsonl(self.output / "case_results.jsonl") if r["experiment_group"] == "D2"))
        cond = row["task_results"][0]["learned_poisoned"]
        cond.update(parse_success=False, predicted_answer=None, clean_correct=False, poison_target_hit=False,
                    defense_blocked=True, defense_outcome_class="semantic_misaligned", defense_technical_failure=False)
        self.assertTrue(completed(row))
        cond.update(defense_outcome_class="parse_error", defense_technical_failure=True)
        self.assertFalse(completed(row))

    def test_reference_rejects_mode_and_task_mismatch(self):
        config = json.loads((self.reference / "run_config.json").read_text())
        ids = json.loads(self.manifest.read_text())["case_ids"][:2]
        source = {c["case_id"]: c for c in read_jsonl(self.cases)}
        cases = [source[cid] for cid in ids]
        with self.assertRaisesRegex(ValueError, "mix live reference"):
            read_reference(self.reference, cases, source_hash=config["source_sha256"], mode="live")
        changed = copy.deepcopy(cases)
        changed[0]["evaluation"]["trigger_tasks"][0]["clean_answer"] = "INVALID"
        with self.assertRaisesRegex(ValueError, "Task identity"):
            read_reference(self.reference, changed, source_hash=config["source_sha256"], mode="mock")

    def test_missing_cell_withholds_metrics(self):
        out = self.directory / "incomplete"
        out.mkdir()
        for name in ("run_config.json", "call_events.jsonl"):
            (out / name).write_bytes((self.output / name).read_bytes())
        rows = read_jsonl(self.output / "case_results.jsonl")
        rows = [r for r in rows if not (r["experiment_group"] == "D3" and r["case_id"] == rows[0]["case_id"])]
        (out / "case_results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        s = analyze(out)
        self.assertFalse(s["complete"])
        self.assertIsNone(s["groups"]["D3"]["fixed_metrics"]["causal_asr"])
        self.assertIsNone(s["groups"]["D3"]["recovery"]["rate"])

    def test_resume_reexecutes_only_one_technical_failure(self):
        damaged = self.directory / "damaged"
        damaged.mkdir()
        for name in ("run_config.json", "call_events.jsonl"):
            (damaged / name).write_bytes((self.output / name).read_bytes())
        rows = read_jsonl(self.output / "case_results.jsonl")
        row = next(r for r in rows if r["experiment_group"] == "D2")
        row["task_results"][0]["learned_poisoned"].update(
            parse_success=False, defense_technical_failure=True, defense_outcome_class="parse_error")
        (damaged / "case_results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        out = self.directory / "recovered"
        execution = self.invoke("compare_external", *self.arguments(), "--resume-from", damaged, "--output-dir", out)
        self.assertIn("pending=1", execution.stdout)
        summary = json.loads((out / "summary.json").read_text())
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["new_run_cost"]["api_attempts"], 0)

    def test_external_roles_preserve_nonthinking_wire_settings(self):
        import httpx
        from openai import OpenAI
        from memform.eval.core import DiskCache
        from experiments.transition_guard_p4.external_defenses import make_defense
        bodies = []
        def handle(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "offline", "object": "chat.completion", "created": 0,
                "model": "guard-test", "choices": [{"index": 0, "message": {"role": "assistant", "content": "[]"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}})
        with tempfile.TemporaryDirectory() as tmp, httpx.Client(transport=httpx.MockTransport(handle)) as http:
            base = "https://offline.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
            client = OpenAI(api_key="offline-placeholder", base_url=base, http_client=http)
            config = {"guard_model": "guard-test", "timeout": 30, "guard_max_output_tokens": 8192,
                      "guard_output_token_field": "auto", "guard_thinking_mode": "off"}
            for group in ("D1", "D2", "D3"):
                defense = make_defense(group, config=config, mode="live", cache=DiskCache(Path(tmp) / "cache"),
                    events=[], scope={}, event_path=Path(tmp) / "events", base_url=base, api_key="offline-placeholder")
                defense.model.backend.client.client = client.chat.completions
                defense.invoke_llm("Check runtime content.", "Ordinary experience.")
        self.assertEqual(len(bodies), 3)
        for body in bodies:
            self.assertFalse(body["enable_thinking"])
            self.assertEqual(body["max_completion_tokens"], 8192)
            self.assertEqual(body["model"], "guard-test")
            self.assertEqual(body["temperature"], 0.0)
