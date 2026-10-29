# FTS optimization on the official competition branch

Base: `upstream/2026-competition` at
`5bce9e06d1281347ad61344e4b01fab9158c58a2`.
Submission branch: `codex/fts-competition-submit`.

The only changed production source is `src/storage/fts/ob_fts_stop_word.cpp`.
It removes a temporary arena from UTF-8 stopword lookups and uses one hash-table
operation for word aggregation. Aggregation preserves the original initial
count, subsequent frequency increments, and last equivalent word's bytes.
The existing build, server startup, official benchmark and scorer are unchanged.

## Linux build and focused tests

Use the competition branch's existing build commands:

```sh
./build.sh release --init
make -C build_release -j2
```

The focused C++ tests link the existing `oceanbase` target and reuse the Storage
target's generated compiler flags. They do not enter the production build:

```sh
make -C build_release -j2 oceanbase
python3 unittest/storage/fts/run_unit.py --build-dir build_release
```

These six cases cover frequency accumulation, equivalent-word replacement,
hash collisions, map reuse, filtering, invalid inputs, duplicate errors when
grouping is disabled, and the supported/unsupported stopword collations.

## Full SQL result comparison

Install PyMySQL in a test Python environment and start a dedicated instance
using the original competition binary. Save its results, then run the optimized
binary in a fresh instance with the same configuration and compare:

```sh
python3 unittest/storage/fts/sql_differential.py --port 12994 \
  --output baseline-results.json
python3 unittest/storage/fts/sql_differential.py --port 12994 \
  --reference baseline-results.json --output candidate-results.json
```

The runner creates and removes a uniquely named test database. It checks full
token arrays and frequencies for IK, BENG, space and ngram, including concurrent
connections; it also compares full query IDs, scores and ordering for IK/BENG
indexes before and after inserts, updates, deletes, and index rebuilding.
The corpus includes seeded mixed-script documents, empty/NULL inputs, control
characters, repeated terms, non-BMP characters, and token-size boundaries.

## Official performance benchmark

Use a dedicated instance: the original script drops `fts_large_bench`. Set
`MYSQL` and `MYSQL_VERBOSE` for its MySQL client and port, then run from the
repository root with a new report path:

```sh
LABEL=candidate ROWS=20000 BATCH=500 ROUNDS=3000 QUERY_ROUNDS=200 \
  SAMPLES=3 WARMUP=30 SKIP_LOAD=0 OUTPUT=/tmp/fts-candidate-report.txt \
  bash tools/benchmark/fts_large_bench.sh
python3 tools/benchmark/fts_large_bench_score.py /tmp/fts-candidate-report.txt
```

Linux container validation cannot establish whether an external judge can clone
the registered repository or which runner it uses. Judge success must be
confirmed by an actual submission of the new branch and its exact commit.
