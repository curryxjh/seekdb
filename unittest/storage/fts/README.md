# FTS hot-path regression and reproduction

This change starts at upstream/master commit
`6ff9b040095ec810f4b14bf7edc022483d85e39b` on `codex/fts-tuning-clean`.
No implementation or test files were copied from the previous competition branch.

## Design

The selected changes are in `ObAddWord::groupby_word` and
`ObStopWordChecker::check_stopword`, shared by tokenization and index construction.

* The original aggregation path looks up a word and then hashes and traverses the
  same bucket again to overwrite its count. The overwrite also allocates a new
  hash node and frees the old one. `set_or_update` does one lookup and updates an
  existing node under the bucket write lock. It preserves the original initial
  count of 1, subsequent frequency increments, and **last equivalent word's
  bytes**, including case/accent-insensitive equivalence. Non-grouping behavior
  and insertion failures continue to use the container's existing paths.
* For supported UTF-8 collations, `charset_convert` already aliases its input
  when converting to the UTF-8 stopword dictionary. This path can query the
  dictionary without constructing a temporary arena. The lookup still uses the
  dictionary's `utf8mb4_general_ci` collation. Other inputs take the original
  conversion path, with the arena alive until the lookup finishes. Unsupported
  collations continue to fail.

There is no new shared mutable cache, change to token rules, SQL planner,
dictionary content, index format, benchmark SQL, data, or scoring logic.

The work investigated the sampling approach described in
[Brendan Gregg's CPU flame graphs](https://www.brendangregg.com/FlameGraphs/cpuflamegraphs.html).
`perf stat -e cpu-clock true` was denied by this Docker VM, including a privileged
exec. Consequently there is **no sampled profile** supporting a claim that these
are the largest bottlenecks. The code establishes the redundant operations;
repeated end-to-end measurements determine their practical impact. This is a
bounded optimization, not a claim of the competition's 50% target.

## Linux correctness tests

After preparing dependencies with the repository's normal Linux build setup:

```sh
python3 build.py build -j2 seekdb
python3 build.py test -j2 //unittest/storage/fts:test_word_aggregation
```

The C++ target covers first-occurrence versus subsequent frequencies, hash
collisions, case-insensitive equality and last-word replacement, map reuse,
invalid inputs, duplicate errors when grouping is disabled, case conversion,
token size filtering, uninitialized maps, stopword dictionary collation, binary
conversion, and rejection of unsupported collations. The last-word assertion
was verified to fail against an intermediate implementation that only updated
the count, then pass with key replacement restored.

For integration testing, install PyMySQL in a test environment and start a
dedicated seekdb instance. Run the same script against the separately built
original and candidate binaries, sequentially:

```sh
python3 unittest/storage/fts/sql_differential.py --port 12992 \
  --output baseline-results.json
python3 unittest/storage/fts/sql_differential.py --port 12992 \
  --reference baseline-results.json --output candidate-results.json
```

`FTS_TEST_PASSWORD` supplies an optional password. The runner creates and removes
its own uniquely named database. It compares complete JSON token outputs and
query rows, not just hit counts. The deterministic corpus includes empty/NULL
inputs, repeated words, mixed scripts, full-width letters, non-BMP characters,
NUL/control characters, token-size boundaries, and seeded random documents.
Both default and ALL token output are covered for IK, BENG, space and ngram.
Four concurrent connections repeat tokenization. IK/BENG index tests cover
natural-language and Boolean queries, multiple columns, scores, sorting,
LIMIT, updates, deletes, inserts, and index rebuilding. Rebuild-stage scores
are compared with the same stage in the baseline: refreshing corpus statistics
can legitimately change them relative to the pre-rebuild stage.

## Unmodified official benchmark

This upstream master uses Bazel and does not contain the contest's benchmark
files. Obtain them from the **official** competition commit independently:

```sh
git fetch upstream 2026-competition
mkdir -p /tmp/fts-official
git archive 5bce9e06d1281347ad61344e4b01fab9158c58a2 tools/benchmark \
  | tar -x -C /tmp/fts-official
```

Use a dedicated instance; the official script drops its `fts_large_bench`
database. Define `MYSQL` and `MYSQL_VERBOSE` for the client and port, then run
from `/tmp/fts-official`, with a **new** report file for each run:

```sh
LABEL=baseline ROWS=20000 BATCH=500 ROUNDS=3000 QUERY_ROUNDS=200 \
  SAMPLES=3 WARMUP=30 SKIP_LOAD=0 OUTPUT=/tmp/fts-baseline-1.txt \
  bash tools/benchmark/fts_large_bench.sh
python3 tools/benchmark/fts_large_bench_score.py /tmp/fts-baseline-1.txt
```

Repeat with the candidate binary using identical hardware, resource settings,
build options and client configuration. Do not compile during timed runs.
Archive the exact source commit/diff and binary hashes separately: a benchmark
run from an exported archive reports `git_head: unknown`.

## Submission boundary

The tested environment is AlmaLinux 8 **aarch64** under Docker, not native macOS
and not the unavailable official judge. Linux x86-64 CI and the platform's
actual checkout/build entry still need verification before claiming submission
readiness. In particular, current master uses `build.py`; the official
competition branch's older workflow invokes `build.sh`. Without the platform's
logs, neither merging master nor this clean branch can be claimed to resolve
the earlier opaque “execution failed” result.

## Measured results

Both binaries built successfully on AlmaLinux 8.10 / Linux 6.10 aarch64, using
the same SDK and `build.py` defaults. Six C++ cases pass. The SQL runner's full
baseline output repeats identically and matches the final candidate exactly.

The first sequential three-run comparison measured **3.15% overall regression**.
An additional three-pair comparison alternated execution order on two otherwise
idle instances in the same VM. Its medians were:

| Metric | Baseline | Candidate | Time reduction |
| --- | ---: | ---: | ---: |
| build_ik_all_sec | 4.6740 | 4.5770 | 2.08% |
| build_ik_content_sec | 3.7570 | 3.5630 | 5.16% |
| build_beng_en_sec | 2.0410 | 1.9420 | 4.85% |
| tokenize_ik_avg_ms | 0.1138 | 0.1080 | 5.10% |
| tokenize_beng_avg_ms | 0.0825 | 0.0810 | 1.82% |
| query_cn_avg_ms | 0.9411 | 0.9665 | -2.70% |
| query_beng_avg_ms | 1.3747 | 1.3678 | 0.50% |
| query_mixed_avg_ms | 1.0660 | 1.0579 | 0.76% |
| query_limit_avg_ms | 1.1902 | 1.1813 | 0.75% |

The category mean reductions in this latter comparison are 4.03%, 3.46%, and
-0.17%, giving 2.44% overall. These are a descriptive comparison of three full
run medians, **not a modification of the official scoring method**. The differing
sequential and alternating results show that the end-to-end gain is small
relative to environmental variation. It is not evidence of a 50% improvement.
The fixed official scorer accepts the reports and prints 100 on this machine
for both original and candidate; this is not an attributable optimization gain
or an official judge result.

`benchmark_tokenize.py` provides a separate long-document diagnostic, alternating
two instances, discarding two warmup groups, and taking five ten-query groups:

```sh
python3 unittest/storage/fts/benchmark_tokenize.py \
  --baseline-port 12993 --candidate-port 12992 --output long-document.json
```

| Diagnostic | Baseline median ms | Candidate median ms | Reduction |
| --- | ---: | ---: | ---: |
| BENG, repeated words | 2.7648 | 1.9871 | 28.13% |
| BENG, distinct words | 4.7194 | 3.3934 | 28.10% |
| IK, repeated mixed text | 15.6175 | 11.7700 | 24.64% |

This diagnostic makes token processing a larger part of total latency. It is
not a replacement for the official workload. Complete semantic checks are in
`sql_differential.py`; the diagnostic's token-count check alone is insufficient
to establish correctness.
