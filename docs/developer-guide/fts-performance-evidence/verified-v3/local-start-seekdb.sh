#!/usr/bin/env bash
# Start our own seekdb instance on port 2883 with isolated runtime dirs.
set -uo pipefail

BUILD_DIR="${BUILD_DIR:-/home/beaten/seekdb-fts/build_release}"
BASE_DIR="${BASE_DIR:-/home/beaten/fts-runtime}"
PORT="${PORT:-2883}"
OBCLIENT=/home/beaten/seekdb-ai-build/deps/3rd/u01/obclient/bin/obclient

BIN="${BIN:-${BUILD_DIR}/src/observer/seekdb}"

if [[ ! -x "${BIN}" ]]; then
  echo "ERROR: binary not found: ${BIN}"
  exit 1
fi

mkdir -p "${BASE_DIR}"/{store,redo,etc,log,run}

# stop previous instance if any
if [[ -f "${BASE_DIR}/run/seekdb.pid" ]]; then
  old_pid=$(cat "${BASE_DIR}/run/seekdb.pid" 2>/dev/null || true)
  if [[ -n "${old_pid}" ]] && kill -0 "${old_pid}" 2>/dev/null; then
    echo "stopping previous pid ${old_pid}"
    kill "${old_pid}" 2>/dev/null || true
    for i in $(seq 1 30); do
      kill -0 "${old_pid}" 2>/dev/null || break
      sleep 1
    done
  fi
fi

nohup "${BIN}" --nodaemon \
  --base-dir "${BASE_DIR}" \
  --data-dir "${BASE_DIR}/store" \
  --redo-dir "${BASE_DIR}/redo" \
  --port "${PORT}" \
  --parameter datafile_size=2G \
  --parameter datafile_maxsize=8G \
  --parameter log_disk_size=4G \
  --parameter memory_limit=4G \
  >> "${BASE_DIR}/seekdb.out" 2>&1 &

pid=$!
echo "${pid}" > "${BASE_DIR}/run/seekdb.pid"
echo "seekdb started pid=${pid} port=${PORT} base=${BASE_DIR}"

for i in $(seq 1 180); do
  if "${OBCLIENT}" -h127.0.0.1 -P"${PORT}" -uroot -N -s -e "SELECT 1" >/dev/null 2>&1; then
    echo "seekdb ready after ${i}s"
    exit 0
  fi
  if ! kill -0 "${pid}" 2>/dev/null; then
    echo "ERROR: seekdb exited early; tail of log:"
    tail -40 "${BASE_DIR}/seekdb.out"
    exit 1
  fi
  sleep 1
done

echo "ERROR: seekdb not ready in 180s; tail of log:"
tail -40 "${BASE_DIR}/seekdb.out"
exit 1
