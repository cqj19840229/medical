#!/usr/bin/env bash
set -euo pipefail

OUT_DIR="${OUT_DIR:-/data/drug_fragment/build_005}"
PID_FILE="${PID_FILE:-${OUT_DIR}/fragment_multi_v5_cpp.pid}"
WATCHDOG_PID_FILE="${WATCHDOG_PID_FILE:-${OUT_DIR}/run_multi_v5_cpp_watchdog.pid}"
STOP_GRACE_SECONDS="${STOP_GRACE_SECONDS:-300}"
FORCE_KILL_AFTER_TIMEOUT="${FORCE_KILL_AFTER_TIMEOUT:-1}"

echo "OUT_DIR=${OUT_DIR}"
echo "STOP_GRACE_SECONDS=${STOP_GRACE_SECONDS}"
echo

echo "=== current processes before stop ==="
ps -eo pid,ppid,pgid,etimes,%cpu,rss,args \
| egrep 'run_multi_v5_cpp_watchdog|fragment_multi_v5_cpp.py|watchdog.singleton.lock' \
| grep -v grep || true

echo
echo "=== stop watchdog first ==="

if [[ -f "${WATCHDOG_PID_FILE}" ]]; then
  WATCHDOG_PID="$(cat "${WATCHDOG_PID_FILE}" 2>/dev/null || true)"
  if [[ -n "${WATCHDOG_PID}" ]] && kill -0 "${WATCHDOG_PID}" 2>/dev/null; then
    echo "TERM watchdog pid=${WATCHDOG_PID}"
    kill -TERM "${WATCHDOG_PID}" 2>/dev/null || true
  fi
fi

# 兼容旧的外层 flock 启动方式。
pkill -TERM -f "flock -n ${OUT_DIR}/watchdog.singleton.lock" 2>/dev/null || true
pkill -TERM -f "run_multi_v5_cpp_watchdog.sh" 2>/dev/null || true

sleep 5

echo
echo "=== terminate fragment process groups ==="

python - <<PY
import os
import signal
import subprocess
import time

stop_grace_seconds = int("${STOP_GRACE_SECONDS}")
force_kill = "${FORCE_KILL_AFTER_TIMEOUT}" == "1"

out = subprocess.check_output(
    ["ps", "-eo", "pid=,ppid=,pgid=,cmd="],
    text=True,
)

rows = []
for line in out.splitlines():
    if "fragment_multi_v5_cpp.py" not in line:
        continue
    parts = line.split(None, 3)
    if len(parts) < 4:
        continue
    pid, ppid, pgid, cmd = parts
    rows.append((int(pid), int(ppid), int(pgid), cmd))

pids = {row[0] for row in rows}
masters = [row for row in rows if row[1] not in pids]

# 如果没有识别出 master，退化为按唯一 pgid 停止。
if not masters:
    seen = set()
    masters = []
    for pid, ppid, pgid, cmd in rows:
        if pgid in seen:
            continue
        seen.add(pgid)
        masters.append((pid, ppid, pgid, cmd))

if not masters:
    print("no fragment process found")
    raise SystemExit(0)

for pid, ppid, pgid, cmd in masters:
    print(f"TERM fragment process group pgid={pgid} master_pid={pid}")
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError as exc:
        print(f"permission error pgid={pgid}: {exc}")

deadline = time.time() + stop_grace_seconds
while time.time() < deadline:
    alive = []
    for pid, ppid, pgid, cmd in masters:
        try:
            os.killpg(pgid, 0)
            alive.append((pid, pgid))
        except ProcessLookupError:
            pass
    if not alive:
        print("all fragment process groups exited")
        break
    print("still alive:", alive)
    time.sleep(10)
else:
    print("grace timeout reached")
    if force_kill:
        for pid, ppid, pgid, cmd in masters:
            print(f"KILL fragment process group pgid={pgid} master_pid={pid}")
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                print(f"permission error pgid={pgid}: {exc}")
    else:
        print("FORCE_KILL_AFTER_TIMEOUT=0, skip KILL")
PY

rm -f "${PID_FILE}" "${WATCHDOG_PID_FILE}"

echo
echo "=== current processes after stop ==="
ps -eo pid,ppid,pgid,etimes,%cpu,rss,args \
| egrep 'run_multi_v5_cpp_watchdog|fragment_multi_v5_cpp.py|watchdog.singleton.lock' \
| grep -v grep || true

echo
echo "stop done"
