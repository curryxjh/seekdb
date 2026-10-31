#!/usr/bin/env bash
# Run the fts_large_bench with our local seekdb instance and score it.
# Usage: bench.sh <label> [SKIP_LOAD] [extra env...]
set -uo pipefail

LABEL="${1:-local}"
SKIP_LOAD="${2:-0}"

REPO_DIR="${REPO_DIR:-/home/beaten/seekdb-fts}"
PORT="${PORT:-2883}"
OBCLIENT=/home/beaten/seekdb-ai-build/deps/3rd/u01/obclient/bin/obclient
OUT_DIR="${OUT_DIR:-/home/beaten/fts-results}"
mkdir -p "${OUT_DIR}"

export MYSQL="${OBCLIENT} -h127.0.0.1 -P${PORT} -uroot -N -s --default-character-set=utf8mb4"
export MYSQL_VERBOSE="${OBCLIENT} -h127.0.0.1 -P${PORT} -uroot --default-character-set=utf8mb4"

REPORT="${OUT_DIR}/report_${LABEL}.txt"
LOG="${OUT_DIR}/bench_${LABEL}.log"
rm -f "${REPORT}"

cd "${REPO_DIR}"
LABEL="${LABEL}" ROWS=20000 BATCH=500 ROUNDS=3000 QUERY_ROUNDS=200 SAMPLES=3 \
  WARMUP=30 SKIP_LOAD="${SKIP_LOAD}" OUTPUT="${REPORT}" \
  bash tools/benchmark/fts_large_bench.sh > "${LOG}" 2>&1
rc=$?
echo "bench exit=${rc} report=${REPORT} log=${LOG}"
if [[ ${rc} -eq 0 ]]; then
  python3 tools/benchmark/fts_large_bench_score.py "${REPORT}" | tee "${OUT_DIR}/score_${LABEL}.txt"
fi
exit ${rc}
