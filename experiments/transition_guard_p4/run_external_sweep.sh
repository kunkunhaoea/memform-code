#!/usr/bin/env bash
# Five existing 32-family panels; only D1/D2/D3 execute new model calls.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.."
mode="${1:-live}"
if [[ "$mode" != live && "$mode" != validate ]]; then
  echo "Usage: bash experiments/transition_guard_p4/run_external_sweep.sh [live|validate]" >&2
  exit 2
fi
python_bin="${MEMFORM_PYTHON:-/home/lukun/miniconda3/envs/memform/bin/python}"
for n in 9 50 100 500 1000; do
  case "$n" in
    9) reference=20261118T065642_903024Z_live ;;
    50) reference=20261118T102606_849944Z_live ;;
    100) reference=20261118T103003_042316Z_live ;;
    500) reference=20261118T110021_094897Z_live ;;
    1000) reference=20261118T112816_480415Z_live ;;
  esac
  echo "N=$n: D1 LLM-Judge / D2 Task Shield / D3 A-MemGuard"
  "$python_bin" -B -m experiments.transition_guard_p4.compare_external \
    --mode "$mode" \
    --cases "data/transition_guard_p4_large_history/v1_seed42/N${n}/cases.jsonl" \
    --manifest "data/transition_guard_p4_large_history/v1_seed42/N${n}/selected_cases_32.json" \
    --reference-run "results/transition_guard_p4/$reference" \
    --methods D1 D2 D3
done
