#!/usr/bin/env bash
set -euo pipefail

cd /data/split_smile/v5

export OUT_DIR="${OUT_DIR:-/data/drug_fragment/build_005}"
export TOTAL_WORKERS="${TOTAL_WORKERS:-8}"
export ACTIVE_MOLECULES="${ACTIVE_MOLECULES:-1}"
TAIL_EXTRA_MOLECULES="${TAIL_EXTRA_MOLECULES:-$((TOTAL_WORKERS / 16))}"
if (( TAIL_EXTRA_MOLECULES < 1 )); then
  TAIL_EXTRA_MOLECULES=1
fi
export TAIL_ACTIVE_MOLECULES="${TAIL_ACTIVE_MOLECULES:-$((ACTIVE_MOLECULES + TAIL_EXTRA_MOLECULES))}"
export TAIL_TRIGGER_SHARDS_FACTOR="${TAIL_TRIGGER_SHARDS_FACTOR:-2}"
export TAIL_MOLECULE_SHARDS="${TAIL_MOLECULE_SHARDS:-32}"
export TAIL_MIN_TAIL_MOLECULES="${TAIL_MIN_TAIL_MOLECULES:-1}"
export TAIL_MIN_NONTAIL_MOLECULES="${TAIL_MIN_NONTAIL_MOLECULES:-0}"
export TAIL_RSS_SOFT_MAX_GB="${TAIL_RSS_SOFT_MAX_GB:-320}"
export TAIL_MEM_AVAILABLE_MIN_GB="${TAIL_MEM_AVAILABLE_MIN_GB:-160}"
export TAIL_LOG_INTERVAL_SECONDS="${TAIL_LOG_INTERVAL_SECONDS:-60}"

mkdir -p "${OUT_DIR}"

nohup ./run_multi_v5_cpp_watchdog.sh \
  >> "${OUT_DIR}/watchdog_launcher.out" 2>&1 &

echo "started watchdog launcher pid=$!"
echo "watchdog_log=${OUT_DIR}/fragment_multi_v5_cpp_watchdog.log"
echo "fragment_log=${OUT_DIR}/fragment_multi_v5_cpp.log"
echo "launcher_log=${OUT_DIR}/watchdog_launcher.out"
