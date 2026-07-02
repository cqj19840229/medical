#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PYTHON_BIN="${PYTHON_BIN:-/data/miniforge3/envs/medical/bin/python}"
INPUT_FILE="${INPUT_FILE:-/data/split_smile/molecule_big_now.csv}"
OUT_DIR="${OUT_DIR:-/data/drug_fragment/build_005}"
LOG_FILE="${LOG_FILE:-${OUT_DIR}/fragment_multi_v5_cpp.log}"
WATCHDOG_LOG_FILE="${WATCHDOG_LOG_FILE:-${OUT_DIR}/fragment_multi_v5_cpp_watchdog.log}"
PID_FILE="${PID_FILE:-${OUT_DIR}/fragment_multi_v5_cpp.pid}"
WATCHDOG_PID_FILE="${WATCHDOG_PID_FILE:-${OUT_DIR}/run_multi_v5_cpp_watchdog.pid}"
WATCHDOG_LOCK_FILE="${WATCHDOG_LOCK_FILE:-${OUT_DIR}/watchdog.singleton.lock}"

TOTAL_WORKERS="${TOTAL_WORKERS:-8}"
ACTIVE_MOLECULES="${ACTIVE_MOLECULES:-1}"
TAIL_EXTRA_MOLECULES="${TAIL_EXTRA_MOLECULES:-$((TOTAL_WORKERS / 16))}"
if (( TAIL_EXTRA_MOLECULES < 1 )); then
  TAIL_EXTRA_MOLECULES=1
fi
TAIL_ACTIVE_MOLECULES="${TAIL_ACTIVE_MOLECULES:-$((ACTIVE_MOLECULES + TAIL_EXTRA_MOLECULES))}"
TAIL_TRIGGER_SHARDS_FACTOR="${TAIL_TRIGGER_SHARDS_FACTOR:-2}"
TAIL_MOLECULE_SHARDS="${TAIL_MOLECULE_SHARDS:-32}"
TAIL_MIN_TAIL_MOLECULES="${TAIL_MIN_TAIL_MOLECULES:-1}"
TAIL_MIN_NONTAIL_MOLECULES="${TAIL_MIN_NONTAIL_MOLECULES:-0}"
TAIL_RSS_SOFT_MAX_GB="${TAIL_RSS_SOFT_MAX_GB:-320}"
TAIL_MEM_AVAILABLE_MIN_GB="${TAIL_MEM_AVAILABLE_MIN_GB:-160}"
TAIL_LOG_INTERVAL_SECONDS="${TAIL_LOG_INTERVAL_SECONDS:-60}"
SHARDS="${SHARDS:-2048}"
BATCH_SIZE="${BATCH_SIZE:-50000}"
MAX_PENDING_TASKS="${MAX_PENDING_TASKS:-16}"
CHECKSUM="${CHECKSUM:-0}"
COMPRESSION="${COMPRESSION:-zstd}"
COMPRESSION_LEVEL="${COMPRESSION_LEVEL:-1}"
FAST_CORE="${FAST_CORE:-cpp}"
CPP_BATCH_SIZE="${CPP_BATCH_SIZE:-2048}"

MEM_AVAILABLE_MIN_GB="${MEM_AVAILABLE_MIN_GB:-10}"
SWAP_USED_MAX_GB="${SWAP_USED_MAX_GB:-3}"
RSS_MAX_GB="${RSS_MAX_GB:-50}"
CHECK_INTERVAL_SECONDS="${CHECK_INTERVAL_SECONDS:-20}"
GRACE_SECONDS="${GRACE_SECONDS:-300}"
RESTART_DELAY_SECONDS="${RESTART_DELAY_SECONDS:-120}"
WATCHDOG_RESTART_ON_EXIT="${WATCHDOG_RESTART_ON_EXIT:-1}"

export MALLOC_ARENA_MAX="${MALLOC_ARENA_MAX:-2}"
export MALLOC_TRIM_THRESHOLD_="${MALLOC_TRIM_THRESHOLD_:-131072}"

mkdir -p "${OUT_DIR}" "$(dirname "${LOG_FILE}")" "$(dirname "${WATCHDOG_LOG_FILE}")"

log_watchdog() {
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "${WATCHDOG_LOG_FILE}" >&2
}

# 内置单例锁：无论直接执行 watchdog 还是通过 start.sh 执行，都不会重复启动。
exec 9>"${WATCHDOG_LOCK_FILE}"
if ! flock -n 9; then
  log_watchdog "action=skip reason=watchdog_already_running lock_file=${WATCHDOG_LOCK_FILE}"
  exit 1
fi

echo "$$" > "${WATCHDOG_PID_FILE}"
log_watchdog "action=watchdog_start pid=$$ lock_file=${WATCHDOG_LOCK_FILE}"

JOB_PID=""
CURRENT_JOB_PID=""

cleanup_watchdog() {
  rm -f "${WATCHDOG_PID_FILE}"
}
trap cleanup_watchdog EXIT

mem_available_kb() {
  awk '/^MemAvailable:/ {print $2}' /proc/meminfo
}

swap_used_kb() {
  awk '
    /^SwapTotal:/ {total=$2}
    /^SwapFree:/ {free=$2}
    END {print total-free}
  ' /proc/meminfo
}

gb_to_kb() {
  awk -v gb="$1" 'BEGIN {printf "%.0f", gb * 1024 * 1024}'
}

process_group_rss_kb() {
  local pgid="$1"
  ps -o rss= -g "${pgid}" 2>/dev/null | awk '{sum += $1} END {print sum + 0}'
}

process_alive_non_zombie() {
  local pid="$1"
  local stat=""

  kill -0 "${pid}" 2>/dev/null || return 1
  stat="$(ps -o stat= -p "${pid}" 2>/dev/null | awk '{print $1}' || true)"

  [[ -n "${stat}" ]] || return 1
  [[ "${stat:0:1}" != "Z" ]]
}

terminate_group() {
  local pgid="$1"
  local reason="$2"

  log_watchdog "action=terminate pgid=${pgid} reason=${reason}"
  kill -TERM "-${pgid}" 2>/dev/null || true

  local waited=0
  while kill -0 "-${pgid}" 2>/dev/null; do
    if (( waited >= GRACE_SECONDS )); then
      log_watchdog "action=kill pgid=${pgid} reason=grace_timeout"
      kill -KILL "-${pgid}" 2>/dev/null || true
      break
    fi
    sleep 5
    waited=$((waited + 5))
  done
}

shutdown_watchdog() {
  local sig="${1:-TERM}"

  log_watchdog "action=watchdog_shutdown signal=${sig}"

  if [[ -n "${CURRENT_JOB_PID:-}" ]] && kill -0 "${CURRENT_JOB_PID}" 2>/dev/null; then
    terminate_group "${CURRENT_JOB_PID}" "watchdog_shutdown_${sig}"
  elif [[ -f "${PID_FILE}" ]]; then
    local old_pid
    old_pid="$(cat "${PID_FILE}" 2>/dev/null || true)"
    if [[ -n "${old_pid}" ]] && kill -0 "${old_pid}" 2>/dev/null; then
      terminate_group "${old_pid}" "watchdog_shutdown_${sig}_pid_file"
    fi
  fi

  rm -f "${PID_FILE}"
  exit 0
}

trap 'shutdown_watchdog TERM' TERM
trap 'shutdown_watchdog INT' INT

start_job() {
  local args=(
    "${SCRIPT_DIR}/fragment_multi_v5_cpp.py"
    --input "${INPUT_FILE}"
    --smiles-col smiles
    --id-col molecule_id
    --out-dir "${OUT_DIR}"
    --total-workers "${TOTAL_WORKERS}"
    --active-molecules "${ACTIVE_MOLECULES}"
    --tail-active-molecules "${TAIL_ACTIVE_MOLECULES}"
    --tail-trigger-shards-factor "${TAIL_TRIGGER_SHARDS_FACTOR}"
    --tail-molecule-shards "${TAIL_MOLECULE_SHARDS}"
    --tail-min-tail-molecules "${TAIL_MIN_TAIL_MOLECULES}"
    --tail-min-nontail-molecules "${TAIL_MIN_NONTAIL_MOLECULES}"
    --tail-rss-soft-max-gb "${TAIL_RSS_SOFT_MAX_GB}"
    --tail-mem-available-min-gb "${TAIL_MEM_AVAILABLE_MIN_GB}"
    --tail-log-interval-seconds "${TAIL_LOG_INTERVAL_SECONDS}"
    --shards "${SHARDS}"
    --batch-size "${BATCH_SIZE}"
    --max-pending-tasks "${MAX_PENDING_TASKS}"
    --compression "${COMPRESSION}"
    --compression-level "${COMPRESSION_LEVEL}"
    --dedupe none
    --key-mode state
    --fast-core "${FAST_CORE}"
    --cpp-batch-size "${CPP_BATCH_SIZE}"
    --with-smiles
  )

  if [[ "${CHECKSUM}" == "1" ]]; then
    args+=(--checksum)
  fi

  setsid "${PYTHON_BIN}" "${args[@]}" >> "${LOG_FILE}" 2>&1 &
  JOB_PID=$!
  CURRENT_JOB_PID="${JOB_PID}"

  echo "${JOB_PID}" > "${PID_FILE}"

  log_watchdog "action=start pid=${JOB_PID} total_workers=${TOTAL_WORKERS} active_molecules=${ACTIVE_MOLECULES} tail_active_molecules=${TAIL_ACTIVE_MOLECULES} tail_trigger_shards_factor=${TAIL_TRIGGER_SHARDS_FACTOR} tail_molecule_shards=${TAIL_MOLECULE_SHARDS} tail_min_tail_molecules=${TAIL_MIN_TAIL_MOLECULES} tail_min_nontail_molecules=${TAIL_MIN_NONTAIL_MOLECULES} shards=${SHARDS} batch_size=${BATCH_SIZE} max_pending_tasks=${MAX_PENDING_TASKS} cpp_batch_size=${CPP_BATCH_SIZE}"
}

main() {
  local min_available_kb
  local max_swap_kb
  local max_rss_kb

  min_available_kb="$(gb_to_kb "${MEM_AVAILABLE_MIN_GB}")"
  max_swap_kb="$(gb_to_kb "${SWAP_USED_MAX_GB}")"
  max_rss_kb="$(gb_to_kb "${RSS_MAX_GB}")"

  while true; do
    local pid
    local rc=0

    JOB_PID=""
    CURRENT_JOB_PID=""
    start_job
    pid="${JOB_PID}"

    while process_alive_non_zombie "${pid}"; do
      local available_kb
      local swap_kb
      local rss_kb

      available_kb="$(mem_available_kb)"
      swap_kb="$(swap_used_kb)"
      rss_kb="$(process_group_rss_kb "${pid}")"

      log_watchdog "status pid=${pid} rss_gb=$(awk -v kb="${rss_kb}" 'BEGIN {printf "%.2f", kb/1024/1024}') mem_available_gb=$(awk -v kb="${available_kb}" 'BEGIN {printf "%.2f", kb/1024/1024}') swap_used_gb=$(awk -v kb="${swap_kb}" 'BEGIN {printf "%.2f", kb/1024/1024}')"

      if (( available_kb < min_available_kb )); then
        terminate_group "${pid}" "mem_available_below_${MEM_AVAILABLE_MIN_GB}gb"
        break
      fi

      if (( swap_kb > max_swap_kb )); then
        terminate_group "${pid}" "swap_used_above_${SWAP_USED_MAX_GB}gb"
        break
      fi

      if (( rss_kb > max_rss_kb )); then
        terminate_group "${pid}" "rss_above_${RSS_MAX_GB}gb"
        break
      fi

      sleep "${CHECK_INTERVAL_SECONDS}"
    done

    wait "${pid}" || rc=$?
    rm -f "${PID_FILE}"
    CURRENT_JOB_PID=""

    log_watchdog "action=exit pid=${pid} rc=${rc}"

    if (( rc == 0 )); then
      log_watchdog "action=complete"
      exit 0
    fi

    if [[ "${WATCHDOG_RESTART_ON_EXIT}" != "1" ]]; then
      exit "${rc}"
    fi

    log_watchdog "action=restart_after_delay seconds=${RESTART_DELAY_SECONDS}"
    sleep "${RESTART_DELAY_SECONDS}"
  done
}

main "$@"
