"""Verify actual SDK payloads, cache isolation and experiment-mode identity offline."""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from experiments.transition_guard_p4.check_output_limit import offline_probe
from experiments.transition_guard_p4.runtime import make_models
from memform.common import read_jsonl
from memform.defenses.transition_guard.adapter import p4_transition
from memform.eval.core import CachedChatModel, DiskCache, CacheOnlyMiss, build_batch_memory
from memform.llm_compat import thinking_parameters


ROOT = Path(__file__).resolve().parents[1]


class NonThinkingTests(unittest.TestCase):
    def test_probe_emits_provider_specific_disable_flag(self):
        for base, name, flag in [
            ("https://api.deepseek.com", "deepseek-flash", {"thinking": {"type": "disabled"}}),
            ("https://ws-test.cn-beijing.maas.aliyuncs.com/compatible-mode/v1", "deepseek-v4.1-flash-1017", {"enable_thinking": False}),
        ]:
            result = offline_probe(base, name, thinking_mode="off")
            for key, value in flag.items():
                self.assertEqual(result["wire_parameters"][key], value)
            self.assertEqual(result["response_metadata"]["reasoning_tokens"], 0)
            self.assertEqual(result["response_metadata"]["requested_thinking_mode"], "off")

    def test_all_roles_send_disable_flag_even_without_output_cap(self):
        import httpx
        from openai import OpenAI
        for base, flag in [("https://api.deepseek.com", {"thinking": {"type": "disabled"}}),
                           ("https://ws-test.cn-beijing.maas.aliyuncs.com/compatible-mode/v1", {"enable_thinking": False})]:
            bodies = []
            def handle(request):
                bodies.append(json.loads(request.content))
                return httpx.Response(200, json={"id": "mock", "object": "chat.completion", "created": 0,
                    "model": "test-model", "choices": [{"index": 0, "message": {"role": "assistant", "content": "answer"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7,
                              "completion_tokens_details": {"reasoning_tokens": 0}}})
            with tempfile.TemporaryDirectory() as tmp, httpx.Client(transport=httpx.MockTransport(handle)) as http:
                sdk = OpenAI(api_key="offline-placeholder", base_url=base, http_client=http)
                models = make_models(mode="live", model="test-model", guard_model="test-model", base_url=base,
                    api_key="offline-placeholder", cache=DiskCache(Path(tmp)/"cache.jsonl"), events=[], scope={},
                    event_path=Path(tmp)/"events.jsonl", timeout=None, temperature=0,
                    guard_max_output_tokens=64, thinking_mode="off")
                with contextlib.redirect_stdout(io.StringIO()):
                    for role, tracked in models.items():
                        tracked.backend.client.client = sdk.chat.completions
                        tracked.backend.invoke(role, "Test request")
                        self.assertEqual(tracked.last_call_metadata["reasoning_tokens"], 0)
                self.assertEqual(len(bodies), 7)
                for role, body in zip(models, bodies):
                    for key, value in flag.items():
                        self.assertEqual(body[key], value, role)
                    self.assertNotIn("reasoning_effort", body)
                    self.assertEqual(bool(set(body) & {"max_tokens", "max_completion_tokens"}), role not in {"agent", "memory"})

    def test_mixed_roles_and_original_cf_updater_use_correct_wire_settings(self):
        import httpx
        from openai import OpenAI
        case = next(c for c in read_jsonl(ROOT / "outputs/mcq_item_audit/accepted_cases.jsonl")
                    if c["interface"] == "P4" and c["evaluation"]["type"] == "multiple_choice")
        for base, cap_field in [("https://api.deepseek.com", "max_tokens"),
                                ("https://ws-test.cn-beijing.maas.aliyuncs.com/compatible-mode/v1", "max_completion_tokens")]:
            with self.subTest(base=base), tempfile.TemporaryDirectory() as tmp:
                bodies, events = [], []
                def handle(request):
                    bodies.append(json.loads(request.content))
                    return httpx.Response(200, json={"id": "mock", "object": "chat.completion", "created": 0,
                        "model": "test-model", "choices": [{"index": 0, "message": {"role": "assistant", "content": "policy"}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}})
                with httpx.Client(transport=httpx.MockTransport(handle)) as http:
                    sdk = OpenAI(api_key="offline-placeholder", base_url=base, http_client=http)
                    cache = DiskCache(Path(tmp) / "cache.jsonl")
                    models = make_models(mode="live", model="test-model", guard_model="guard-model", base_url=base,
                        api_key="offline-placeholder", cache=cache, events=events, scope={},
                        event_path=Path(tmp) / "events.jsonl", timeout=None, temperature=0,
                        guard_max_output_tokens=64, agent_thinking_mode="on", memory_thinking_mode="on",
                        guard_thinking_mode="off")
                    with contextlib.redirect_stdout(io.StringIO()):
                        for role, tracked in models.items():
                            tracked.backend.client.client = sdk.chat.completions
                            tracked.invoke(role, "Test request")
                        ids = case["memory_building"]["clean_condition"]["order"]
                        memory = build_batch_memory(models["memory"], case, ids, case["memory_building"]["batch_prompt"], 120)
                        transition, replay = p4_transition(case, ids, memory, models["memory"], 120)
                        selected = transition.selected_experiences
                        omitted = transition.omitted_experiences[0]
                        replay((omitted,) + selected[1:])
                        off = CachedChatModel("memory", "test-model", base, None, cache,
                                              cache_only=True, thinking_mode="off")
                        with self.assertRaises(CacheOnlyMiss):
                            off.invoke("memory", "Test request")
                    self.assertEqual(len(bodies), 9)
                    self.assertEqual([e["stage"] for e in events[-2:]], ["memory", "counterfactual_replay"])
                    for event, body in zip(events, bodies):
                        target = event["role"] in {"agent", "memory"}
                        flag = ({"thinking": {"type": "enabled" if target else "disabled"}}
                                if "deepseek.com" in base else {"enable_thinking": target})
                        for key, value in flag.items():
                            self.assertEqual(body[key], value, event["role"])
                        self.assertEqual(event["response_metadata"]["requested_thinking_mode"], "on" if target else "off")
                        self.assertEqual(body["model"], "test-model" if target else "guard-model")
                        self.assertNotIn("reasoning_effort", body)
                        if target:
                            self.assertNotIn("max_tokens", body)
                            self.assertNotIn("max_completion_tokens", body)
                        else:
                            self.assertEqual(body[cap_field], 64)
                    self.assertEqual({k: v for k, v in bodies[-2].items() if k != "messages"},
                                     {k: v for k, v in bodies[-1].items() if k != "messages"})

    def test_default_and_nonthinking_cache_entries_cannot_mix(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            cache = DiskCache(Path(tmp)/"cache.jsonl")
            payload = dict(model="test-model", base_url="https://api.deepseek.com", system="system", user="user",
                           temperature=0.0, reasoning_effort=None, cache_namespace="")
            old_key = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            cache.put(old_key, "old thought-mode response", {"usage": None})
            old = CachedChatModel("agent", "test-model", "https://api.deepseek.com", None, cache, cache_only=True)
            self.assertEqual(old.invoke("system", "user"), "old thought-mode response")
            disabled = CachedChatModel("agent", "test-model", "https://api.deepseek.com", None, cache,
                                       cache_only=True, thinking_mode="off")
            with self.assertRaises(CacheOnlyMiss):
                disabled.invoke("system", "user")

    def test_unknown_endpoint_cannot_silently_ignore_thinking_switch(self):
        self.assertEqual(thinking_parameters("https://custom.example", "default"), {})
        with self.assertRaises(ValueError):
            thinking_parameters("https://custom.example", "off")

    def test_cli_mixed_defaults_legacy_baseline_and_role_resume_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            common = [sys.executable,"-B","-m","experiments.transition_guard_p4.run_experiment",
                      "--mode","mock","--model","test-model","--sample-size","16","--limit","1",
                      "--cache",str(root/"cache.jsonl")]
            source = root/"source"
            first = subprocess.run(common+["--groups","B0","--thinking","on","--output-dir",str(source)],
                                   cwd=ROOT,capture_output=True,text=True)
            self.assertEqual(first.returncode,0,first.stderr)
            # A historical uniform-on B0 is valid for mixed mode when both
            # target roles match; guard settings do not belong to B0 identity.
            legacy = json.loads((source/"run_config.json").read_text())
            for role in ("agent", "memory", "guard"):
                legacy.pop(role + "_thinking_mode")
            (source/"run_config.json").write_text(json.dumps(legacy))
            dest=root/"mixed"
            second=subprocess.run(common+["--baseline-from",str(source),"--output-dir",str(dest)],
                                  cwd=ROOT,capture_output=True,text=True)
            self.assertEqual(second.returncode,0,second.stderr)
            config=json.loads((dest/"run_config.json").read_text())
            self.assertEqual(config["thinking_mode"],"mixed")
            for role, mode in (("agent", "on"), ("memory", "on"), ("guard", "off")):
                self.assertEqual(config[role + "_thinking_mode"], mode)
            resumed=subprocess.run(common+["--resume-from",str(dest),"--output-dir",str(root/"resumed")],
                                   cwd=ROOT,capture_output=True,text=True)
            self.assertEqual(resumed.returncode,0,resumed.stderr)
            self.assertIn("retained=1 pending=0",resumed.stdout)
            for role in ("agent", "memory"):
                wrong = root/("wrong_baseline_"+role)
                mismatch = subprocess.run(common+["--baseline-from",str(source),"--"+role+"-thinking","off",
                                                  "--output-dir",str(wrong)],cwd=ROOT,capture_output=True,text=True)
                self.assertNotEqual(mismatch.returncode,0)
                self.assertIn(role+"_thinking_mode",mismatch.stderr)
                self.assertFalse(wrong.exists())
            resume=subprocess.run(common+["--guard-thinking","on","--resume-from",str(dest),"--output-dir",str(root/"wrong_resume")],
                                  cwd=ROOT,capture_output=True,text=True)
            self.assertNotEqual(resume.returncode,0)
            self.assertIn("guard_thinking_mode",resume.stderr)

    def test_cli_shared_switch_role_overrides_and_guard_effort_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            common=[sys.executable,"-B","-m","experiments.transition_guard_p4.run_experiment",
                    "--mode","validate","--sample-size","16","--limit","1"]
            for name, flags, modes in [
                ("off", ["--thinking","off"], ("off","off","off")),
                ("override", ["--thinking","off","--agent-thinking","on","--memory-thinking","on"], ("on","on","off")),
                ("effort", ["--guard-thinking","on","--guard-reasoning-effort","low"], ("on","on","on")),
            ]:
                dest=root/name
                proc=subprocess.run(common+flags+["--output-dir",str(dest)],cwd=ROOT,capture_output=True,text=True)
                self.assertEqual(proc.returncode,0,proc.stderr)
                config=json.loads((dest/"run_config.json").read_text())
                self.assertEqual(tuple(config[r+"_thinking_mode"] for r in ("agent","memory","guard")),modes)
            invalid=root/"invalid"
            proc=subprocess.run(common+["--guard-reasoning-effort","low","--output-dir",str(invalid)],
                                cwd=ROOT,capture_output=True,text=True)
            self.assertNotEqual(proc.returncode,0)
            self.assertIn("Guard thinking is off",proc.stderr)
            self.assertFalse(invalid.exists())


if __name__ == "__main__":
    unittest.main()
