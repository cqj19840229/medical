#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-C:/Users/56884/anaconda3/envs/medical/python.exe}"
CXX="${CXX:-g++}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "error: PYTHON_BIN is not executable: ${PYTHON_BIN}" >&2
  echo "Linux Conda environments normally use: <conda>/envs/medical/bin/python" >&2
  exit 1
fi

if ! command -v "${CXX}" >/dev/null 2>&1; then
  echo "error: C++ compiler not found: ${CXX}" >&2
  exit 1
fi

if ! PYTHON_CONFIG="$("${PYTHON_BIN}" - <<'PY'
import sysconfig
print(
    sysconfig.get_config_var("EXT_SUFFIX") or ".so",
    sysconfig.get_paths().get("include", ""),
    sysconfig.get_paths().get("platinclude", ""),
    sep="|",
)
PY
)"; then
  echo "error: failed to query Python build configuration from ${PYTHON_BIN}" >&2
  exit 1
fi

IFS='|' read -r EXT_SUFFIX INCLUDE_DIR PLAT_INCLUDE_DIR <<< "${PYTHON_CONFIG}"
SOURCE_FILE="${SCRIPT_DIR}/fast_fragment_core.cpp"

if [[ -z "${INCLUDE_DIR}" || ! -f "${INCLUDE_DIR}/Python.h" ]]; then
  echo "error: Python development header not found: ${INCLUDE_DIR}/Python.h" >&2
  echo "Install the development files matching ${PYTHON_BIN}, then retry." >&2
  exit 1
fi

if [[ ! -f "${SOURCE_FILE}" ]]; then
  echo "error: C++ source file not found: ${SOURCE_FILE}" >&2
  exit 1
fi

INCLUDES=("-I${INCLUDE_DIR}")
if [[ -n "${PLAT_INCLUDE_DIR}" && "${PLAT_INCLUDE_DIR}" != "${INCLUDE_DIR}" ]]; then
  INCLUDES+=("-I${PLAT_INCLUDE_DIR}")
fi

"${CXX}" -O3 -DNDEBUG -std=c++17 -shared -fPIC \
  "${INCLUDES[@]}" \
  "${SOURCE_FILE}" \
  -o "${SCRIPT_DIR}/fast_fragment_core${EXT_SUFFIX}"
echo "built ${SCRIPT_DIR}/fast_fragment_core${EXT_SUFFIX}"
