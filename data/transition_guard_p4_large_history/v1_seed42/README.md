# Reusable P4 large-history stress data

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
