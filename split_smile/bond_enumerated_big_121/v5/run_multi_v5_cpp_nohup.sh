#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PYTHON_BIN="${PYTHON_BIN:-/root/anaconda3/envs/medical/bin/python}"
INPUT_FILE="${INPUT_FILE:-${SCRIPT_DIR}/../molecule_big_after991.csv}"
OUT_DIR="${OUT_DIR:-/mnt/datadisk/drug_fragment/build_005}"
LOG_FILE="${LOG_FILE:-${OUT_DIR}/fragment_multi_v5_cpp.log}"

TOTAL_WORKERS="${TOTAL_WORKERS:-6}"
ACTIVE_MOLECULES="${ACTIVE_MOLECULES:-1}"
SHARDS="${SHARDS:-2048}"
BATCH_SIZE="${BATCH_SIZE:-50000}"
MAX_PENDING_TASKS="${MAX_PENDING_TASKS:-12}"
CHECKSUM="${CHECKSUM:-0}"
COMPRESSION="${COMPRESSION:-zstd}"
COMPRESSION_LEVEL="${COMPRESSION_LEVEL:-1}"
FAST_CORE="${FAST_CORE:-cpp}"
CPP_BATCH_SIZE="${CPP_BATCH_SIZE:-2048}"

export MALLOC_ARENA_MAX="${MALLOC_ARENA_MAX:-2}"
export MALLOC_TRIM_THRESHOLD_="${MALLOC_TRIM_THRESHOLD_:-131072}"

mkdir -p "${OUT_DIR}" "$(dirname "${LOG_FILE}")"

EXTRA_ARGS=(--with-smiles)
if [[ "${CHECKSUM}" == "1" ]]; then
  EXTRA_ARGS+=(--checksum)
fi

nohup "${PYTHON_BIN}" "${SCRIPT_DIR}/fragment_multi_v5_cpp.py" \
  --input "${INPUT_FILE}" \
  --smiles-col smiles \
  --id-col molecule_id \
  --out-dir "${OUT_DIR}" \
  --total-workers "${TOTAL_WORKERS}" \
  --active-molecules "${ACTIVE_MOLECULES}" \
  --shards "${SHARDS}" \
  --batch-size "${BATCH_SIZE}" \
  --max-pending-tasks "${MAX_PENDING_TASKS}" \
  --compression "${COMPRESSION}" \
  --compression-level "${COMPRESSION_LEVEL}" \
  --dedupe none \
  --key-mode state \
  --fast-core "${FAST_CORE}" \
  --cpp-batch-size "${CPP_BATCH_SIZE}" \
  "${EXTRA_ARGS[@]}" \
  > "${LOG_FILE}" 2>&1 &

echo "started pid=$!"
echo "log=${LOG_FILE}"
