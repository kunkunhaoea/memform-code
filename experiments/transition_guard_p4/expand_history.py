"""Materialize reusable P4 large-history stress data without models or API calls.

This is a derived cross-workflow background stress test, not an official
MemForm release or a proof of semantic noninterference. No target-model
outputs, gold policies, support labels, or task answers select backgrounds.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterator

from memform.common import read_jsonl, stable_hash, validate_compiled_case, write_json, write_jsonl


ROOT = Path(__file__).resolve().parents[2]
VERSION = "p4-large-history-v1"
DEFAULT_SOURCE = ROOT / "outputs/mcq_item_audit/accepted_cases.jsonl"
DEFAULT_OUTPUT = ROOT / "data/transition_guard_p4_large_history/v1_seed42"
DEFAULT_SIZES = (9, 50, 100, 500, 1000)
STOP = frozenset("""how to the a an when of for and or in on at with is are be by from
    after before its it this that should what which whether do does as into without
    under during only if has have not no current same handle respond proceed marked
    reported reports""".split())
GENERIC_DOMAIN = frozenset("""operations management workflow processing service system
    internal shared managed request handling maintaining""".split())


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def terms(value: str) -> frozenset[str]:
    words = (word[:-1] if len(word) > 4 and word.endswith("s") else word
             for word in re.findall(r"[a-z]+", value.casefold()))
    return frozenset(words) - STOP


def priority(seed: int, purpose: str, *values: str) -> str:
    return stable_hash([VERSION, seed, purpose, *values])


def load_source(path: Path) -> tuple[list[dict], list[dict]]:
    rows = read_jsonl(path)
    targets = sorted((c for c in rows if c.get("interface") == "P4"
                      and c.get("evaluation", {}).get("type") == "multiple_choice"),
                     key=lambda c: c["case_id"])
    if not targets or len({c["case_id"] for c in targets}) != len(targets):
        raise ValueError("Source requires unique P4 MCQ cases")
    if len({c["family_id"] for c in targets}) != len(targets):
        raise ValueError("v1 requires one P4 case per family")
    for case in targets:
        if len(case["candidate_experiences"]) != 9:
            raise ValueError("v1 expands original N=9 P4 cases only")
        errors = validate_compiled_case(case)
        if errors:
            raise ValueError(f"Invalid source case {case['case_id']}: {errors}")
        pool = {e["id"] for e in case["candidate_experiences"]}
        for side in ("clean", "poisoned"):
            condition = case["memory_building"][side + "_condition"]
            if Counter(condition["order"]) != Counter(condition["selected_ids"]):
                raise ValueError("Source order and selected subset must match")
            if not set(condition["order"]) <= pool:
                raise ValueError("Source selection refers to missing observations")
    return rows, targets


def donor_bank(rows: list[dict]) -> list[dict]:
    """Reuse P1's observation texts, not its injected memory or provenance label.

    P1 covers more families than P4 and stores the original procedural
    observations. Using only this interface avoids P2 forged records and
    repeated copies of the same family across interfaces.
    """
    seen: dict[str, dict] = {}
    for case in sorted((c for c in rows if c.get("interface") == "P1"), key=lambda c: c["case_id"]):
        for experience in sorted(case["candidate_experiences"], key=lambda e: e["id"]):
            text = experience["text"]
            if not text.strip() or not isinstance(experience.get("action"), str) or not experience["action"].strip():
                raise ValueError("Donor observation requires text and an action")
            digest = hashlib.sha256(text.encode()).hexdigest()
            row = {"bank_id": "obs_" + digest, "text_sha256": digest, "text": text,
                   "source_case_id": case["case_id"], "source_family_id": case["family_id"],
                   "source_experience_id": experience["id"], "source_interface": "P1",
                   "domain": case["domain"], "decision_variable": case["decision_variable"],
                   "action": experience["action"]}
            if digest in seen and seen[digest]["text"] != text:
                raise ValueError("Observation hash collision")
            seen.setdefault(digest, row)
    if not seen:
        raise ValueError("Source has no P1 observation bank")
    return sorted(seen.values(), key=lambda d: d["bank_id"])


def family_signatures(bank: list[dict]) -> dict[str, dict]:
    families: dict[str, dict] = {}
    for donor in bank:
        row = families.setdefault(donor["source_family_id"], {
            "domain": terms(donor["domain"]) - GENERIC_DOMAIN,
            "decision": terms(donor["decision_variable"]), "actions": set()})
        row["actions"].add(donor["action"].strip().casefold())
    return families


def exclusion_reason(case: dict, family_id: str, signature: dict) -> str | None:
    if family_id == case["family_id"]:
        return "same_family"
    domain = terms(case["domain"]) - GENERIC_DOMAIN
    if domain & signature["domain"]:
        return "overlapping_domain_terms"
    if len(terms(case["decision_variable"]) & signature["decision"]) >= 2:
        return "overlapping_decision_terms"
    actions = {e["action"].strip().casefold() for e in case["candidate_experiences"]}
    if actions & signature["actions"]:
        return "same_action"
    return None


def make_plan(case: dict, bank: list[dict], count: int, seed: int,
              signatures: dict | None = None) -> dict:
    signatures = signatures or family_signatures(bank)
    excluded_families = {fid: reason for fid, sig in signatures.items()
                         if (reason := exclusion_reason(case, fid, sig)) is not None}
    core_texts = {e["text"] for e in case["candidate_experiences"]}
    eligible = [d for d in bank if d["source_family_id"] not in excluded_families and d["text"] not in core_texts]
    if len(eligible) < count:
        raise ValueError(f"{case['case_id']}: need {count} distinct backgrounds, only {len(eligible)} pass screening")
    query = terms(case["decision_variable"])
    # One in four additions prefers lexical overlap among already screened
    # OTHER workflows; the remainder follows a seeded content-hash order.
    # This is not a certified same-domain adversarial distractor set.
    random_order = sorted(eligible, key=lambda d: priority(seed, "background", case["case_id"], d["bank_id"]))
    lexical_order = sorted(eligible, key=lambda d: (
        -len(query & terms(d["text"])), priority(seed, "lexical", case["case_id"], d["bank_id"])))
    streams = [iter(random_order), iter(lexical_order)]
    chosen, used = [], set()
    while len(chosen) < count:
        stream = streams[int(len(chosen) % 4 == 0)]
        donor = next(d for d in stream if d["bank_id"] not in used)
        chosen.append(donor["bank_id"])
        used.add(donor["bank_id"])
    return {"case_id": case["case_id"], "family_id": case["family_id"],
            "source_case_sha256": stable_hash(case), "donor_bank_ids": chosen,
            "eligible_background_count": len(eligible),
            "excluded_families": excluded_families,
            "exclusion_counts": dict(sorted(Counter(excluded_families.values()).items()))}


def expanded_case(case: dict, plan: dict, bank: dict[str, dict], size: int, seed: int) -> dict:
    if size == 9:
        return copy.deepcopy(case)
    result = copy.deepcopy(case)
    core = result["candidate_experiences"]
    used_ids = {e["id"] for e in core}
    backgrounds, serial = [], 1
    for bank_id in plan["donor_bank_ids"][:size - 9]:
        while "E" + str(serial) in used_ids:
            serial += 1
        eid = "E" + str(serial)
        used_ids.add(eid)
        # Raw records expose neither background labels nor source/gold metadata.
        backgrounds.append({"id": eid, "text": bank[bank_id]["text"]})
    if len(core) + len(backgrounds) != size:
        raise ValueError("Expansion plan is too short")
    result["candidate_experiences"] = core + backgrounds
    full = result["memory_building"]["mechanism_controls"]["full_pool"]
    original_order = list(full["experience_ids"])
    # Keep original full-pool relative order but interleave additions. Priorities
    # do not depend on N, so each smaller order is a subsequence of larger ones.
    slots = sorted(priority(seed, "core_slot", case["case_id"], eid) for eid in original_order)
    priorities = dict(zip(original_order, slots))
    priorities.update({e["id"]: priority(seed, "pool_slot", case["case_id"], e["id"])
                       for e in backgrounds})
    full["experience_ids"] = sorted(used_ids, key=lambda eid: (priorities[eid], eid))
    metadata = result.setdefault("metadata", {})
    metadata["p4_candidate_pool_size"] = size
    metadata["history_expansion"] = {
        "version": VERSION, "seed": seed, "pool_size": size, "core_pool_size": 9,
        "background_policy": "screened_cross_workflow_original_observations",
        "source_case_sha256": plan["source_case_sha256"],
        "source_compiled_case_hash": case.get("metadata", {}).get("compiled_case_hash"),
        "semantic_status": "lexically_screened_not_independently_semantically_certified"}
    metadata.pop("compiled_case_hash", None)
    metadata["compiled_case_hash"] = stable_hash(result)
    errors = validate_compiled_case(result)
    if errors:
        raise ValueError(f"Invalid expanded case {case['case_id']}: {errors}")
    return result


def jsonl_rows(path: Path) -> Iterator[dict]:
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def selection_manifest(case_ids: list[str], indexed: dict[str, dict], source_hash: str, seed: int) -> dict:
    return {"seed": seed, "source": "cases.jsonl", "source_sha256": source_hash,
            "selection_rule": "seeded_content_hash_family_order_no_model_outputs",
            "case_ids": case_ids, "family_ids": [indexed[cid]["family_id"] for cid in case_ids]}


def verify_bundle(output: Path, source: Path) -> dict:
    output, source = output.resolve(), source.resolve()
    manifest = json.loads((output / "manifest.json").read_text())
    if manifest["version"] != VERSION or manifest["source_sha256"] != file_hash(source):
        raise ValueError("Bundle version/source fingerprint mismatch")
    for relative, record in manifest["files"].items():
        path = (output / relative).resolve()
        if not path.is_relative_to(output) or not path.is_file() or file_hash(path) != record["sha256"]:
            raise ValueError(f"Bundle file missing or hash mismatch: {relative}")
    rows, cases = load_source(source)
    donors = donor_bank(rows)
    if read_jsonl(output / "donor_bank.jsonl") != donors:
        raise ValueError("Donor bank differs from source observations")
    bank = {d["bank_id"]: d for d in donors}
    plans = read_jsonl(output / "case_plans.jsonl")
    sizes, seed = manifest["sizes"], manifest["seed"]
    signatures = family_signatures(donors)
    expected_plans = [make_plan(c, donors, max(sizes) - 9, seed, signatures) for c in cases]
    if plans != expected_plans:
        raise ValueError("Expansion plan differs from deterministic screening/selection")
    indexed = {c["case_id"]: c for c in cases}
    chosen = sorted(indexed, key=lambda cid: priority(seed, "sample", indexed[cid]["family_id"]))
    for size in sizes:
        folder = output / f"N{size}"
        count = 0
        for count, actual in enumerate(jsonl_rows(folder / "cases.jsonl"), 1):
            if count > len(cases):
                raise ValueError("Unexpected extra case")
            expected = expanded_case(cases[count - 1], plans[count - 1], bank, size, seed)
            if actual != expected:
                raise ValueError(f"N{size}/{cases[count - 1]['case_id']}: task, core, background or ordering changed")
        if count != len(cases):
            raise ValueError(f"N{size}: missing cases")
        digest = file_hash(folder / "cases.jsonl")
        for name, sample in (("selected_cases.json", chosen),
                             (f"selected_cases_{min(32, len(cases))}.json", chosen[:32])):
            if json.loads((folder / name).read_text()) != selection_manifest(sample, indexed, digest, seed):
                raise ValueError(f"N{size}: selection manifest mismatch")
    return {"status": "verified", "version": VERSION, "cases_per_size": len(cases),
            "sizes": sizes, "source_sha256": manifest["source_sha256"], "api_calls": 0}


def dataset_card() -> str:
    return """# Reusable P4 large-history stress data

This derived dataset keeps the original P4 task/answers, original nine records,
selected subsets, their order, and the original batch prompt unchanged. N9 is an
exact object-level P4-only snapshot of the official mixed-interface source.
N50/N100/N500/N1000 add distinct, unmodified observation texts from other P1
families in that same frozen source. P1 injected policy text is NEVER imported.

Screening excludes the target family, overlapping informative domain terms,
two or more overlapping decision-variable terms, and any family sharing an
exact action with the original core. One in four additions prefers lexical
overlap among eligible other workflows; other additions use a seeded hash order.
Every smaller pool is a subset of larger pools. Full-pool replay interleaves
backgrounds with the core, preserves core relative order, and is nested too.
No model outcomes, tasks, answers, clean/poisoned policies, supports, or success
labels choose donors. No new prose, answer-bearing hints, or background markers
are inserted into observation text. Source metadata lives in donor_bank.jsonl
and case_plans.jsonl, not in model-visible background records.

IMPORTANT LIMIT: lexical/topic screening is not independent semantic certification.
This is a cross-workflow background stress test, not a certified same-domain
adversarial benchmark and not an official MemForm release. It preserves task
labels by construction; whether a model gets confused by background is an
experimental outcome. Donor reuse across target families creates shared history,
not additional independent samples. Review source workflows for semantic overlap
before claiming deployment-level generality. Repeating original records is not
used to reach N; additions have distinct exact text within each pool.

The current v6 adapter anonymizes/sorts the ENTIRE pool, and its RNG depends on
the ENTIRE pool. Changing N can therefore change anonymous handles and swap
positions, even when the chosen witness text is unchanged. v6 is not modified.
Use text hashes for cross-N evidence analysis, and measure local scanning/sorting
alongside model costs. A bounded LLM shortlist does not imply sublinear total cost.

Use --cases N1000/cases.jsonl with the existing P4 runner. selected_cases.json
contains all 144 families; selected_cases_32.json contains a fixed 32-family
prefix shared across N, selected independently of model outcomes. These are
pressure-test families from an already observed benchmark, not unseen test data.
The runner uses --manifest as the authoritative sample list: do not combine a
144-case manifest with --sample-size 32 expecting only 32 cases to run.
Do not use manifests/resume directories from the original N9 source at larger N.

Build once; normal experiment runs only READ these cases. Re-running the builder
with an identical configuration verifies and reuses the existing bundle without
rewriting it. Different configurations or corrupted/incomplete bundles fail;
there is no force/overwrite mode. --verify-only checks hashes, original core,
labels, screening, provenance, deterministic composition and replay order offline.
B2 must read the whole pool; context failure is technical infeasibility, not an
attack success or a safety win. No data generation API is used.
"""


def build_bundle(source: Path, output: Path, sizes: list[int] | tuple[int, ...] = DEFAULT_SIZES,
                 seed: int = 42) -> dict:
    source, output = source.resolve(), output.resolve()
    sizes = sorted(set(sizes) | {9})
    if min(sizes) < 9:
        raise ValueError("Pool sizes must be >=9")
    config = {"version": VERSION, "seed": seed, "sizes": sizes,
              "source_sha256": file_hash(source), "generator_sha256": file_hash(Path(__file__))}
    if output.exists():
        path = output / "manifest.json"
        if not path.is_file():
            raise ValueError("Output exists without a completed manifest; refusing to overwrite")
        old = json.loads(path.read_text())
        if any(old.get(k) != v for k, v in config.items()):
            raise ValueError("Existing bundle configuration differs; use a new output directory")
        return {**verify_bundle(output, source), "status": "reused_without_rewriting", "output_dir": str(output)}
    rows, cases = load_source(source)
    donors = donor_bank(rows)
    signatures = family_signatures(donors)
    plans = [make_plan(c, donors, max(sizes) - 9, seed, signatures) for c in cases]
    bank = {d["bank_id"]: d for d in donors}
    indexed = {c["case_id"]: c for c in cases}
    chosen = sorted(indexed, key=lambda cid: priority(seed, "sample", indexed[cid]["family_id"]))
    # All capacity/selection checks above happen before creating any output.
    output.mkdir(parents=True, exist_ok=False)
    write_jsonl(output / "donor_bank.jsonl", donors)
    write_jsonl(output / "case_plans.jsonl", plans)
    (output / "README.md").write_text(dataset_card(), encoding="utf-8")
    profiles = {}
    for size in sizes:
        folder = output / f"N{size}"
        def expanded_rows() -> Iterator[dict]:
            for case, plan in zip(cases, plans):
                yield expanded_case(case, plan, bank, size, seed)
        write_jsonl(folder / "cases.jsonl", expanded_rows())
        digest = file_hash(folder / "cases.jsonl")
        write_json(folder / "selected_cases.json", selection_manifest(chosen, indexed, digest, seed))
        write_json(folder / f"selected_cases_{min(32, len(cases))}.json",
                   selection_manifest(chosen[:32], indexed, digest, seed))
        word_counts, char_counts = [], []
        for case in jsonl_rows(folder / "cases.jsonl"):
            texts = [e["text"] for e in case["candidate_experiences"]]
            word_counts.append(sum(len(t.split()) for t in texts))
            char_counts.append(sum(len(t) for t in texts))
        profiles[str(size)] = {"cases": len(cases), "pool_records": size,
                               "background_records": size - 9,
                               "observation_words_min": min(word_counts), "observation_words_max": max(word_counts),
                               "observation_chars_min": min(char_counts), "observation_chars_max": max(char_counts)}
    files = {str(path.relative_to(output)): {"sha256": file_hash(path), "bytes": path.stat().st_size}
             for path in sorted(output.rglob("*")) if path.is_file()}
    write_json(output / "manifest.json", {
        **config, "source": str(source), "cases_per_size": len(cases),
        "donor_bank_size": len(donors), "pilot_size": min(32, len(cases)),
        "minimum_eligible_backgrounds": min(p["eligible_background_count"] for p in plans),
        "semantic_status": "lexically_screened_not_independently_semantically_certified",
        "profiles": profiles, "files": files, "api_calls": 0})
    return {**verify_bundle(output, source), "status": "created_and_verified", "output_dir": str(output)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--sizes", type=int, nargs="+", default=list(DEFAULT_SIZES))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    result = (verify_bundle(args.output_dir.resolve(), args.source.resolve()) if args.verify_only else
              build_bundle(args.source, args.output_dir, args.sizes, args.seed))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
