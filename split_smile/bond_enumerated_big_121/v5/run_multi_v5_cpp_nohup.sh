#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PYTHON_BIN="${PYTHON_BIN:-/opt/miniconda3/envs/medical/bin/python}"
INPUT_FILE="${INPUT_FILE:-${SCRIPT_DIR}/molecule_big.csv}"
OUT_DIR="${OUT_DIR:-/mnt/datadisk/drug_fragment/build_002}"
LOG_FILE="${LOG_FILE:-${OUT_DIR}/fragment_multi_v5_cpp.log}"

TOTAL_WORKERS="${TOTAL_WORKERS:-4}"
ACTIVE_MOLECULES="${ACTIVE_MOLECULES:-1}"
SHARDS="${SHARDS:-4096}"
BATCH_SIZE="${BATCH_SIZE:-25000}"
MAX_PENDING_TASKS="${MAX_PENDING_TASKS:-8}"
CHECKSUM="${CHECKSUM:-0}"
COMPRESSION="${COMPRESSION:-zstd}"
COMPRESSION_LEVEL="${COMPRESSION_LEVEL:-1}"
FAST_CORE="${FAST_CORE:-auto}"
CPP_BATCH_SIZE="${CPP_BATCH_SIZE:-1024}"

export MALLOC_ARENA_MAX="${MALLOC_ARENA_MAX:-2}"
export MALLOC_TRIM_THRESHOLD_="${MALLOC_TRIM_THRESHOLD_:-131072}"

mkdir -p "${OUT_DIR}" "$(dirname "${LOG_FILE}")"

EXTRA_ARGS=(--with-smiles)
if [[ "${CHECKSUM}" == "1" ]]; then
  EXTRA_ARGS+=(--checksum)
fi
EXTRA_ARGS+=(--continue-on-error)

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
