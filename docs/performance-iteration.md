# 2026-11-12 performance iteration

The user's 2026-11-11 19:58 submission of
`b3b16af1e8429b3daed14bbdfedc89bf67c6e2a8` passed formal MySQL admission.
Platform raw performance was `1968.948310`; raw Agent evaluation was `1`.
The user requested performance above `5300`. This iteration has not been
submitted. No new official score or rank is claimed. The platform did not expose
T/B/M components or a conversion from local timings to its raw indicator.

## Delivered changes

* Cache one table's vector prefix and row offsets within 8 MiB. Larger tables
  stream the tail exactly. Every mutation, failed statement and rollback
  invalidates cached data before offsets can be reused.
* Retain four packed predicate masks for the prefix, with keys bounded to
  4096 bytes. Other predicates use normal filtering. Query answers are not cached.
* Accumulate squared L2 in independent double lanes; use AVX2 only on supported
  x86_64 CPUs. Convert float32 to double before subtraction. Avoid square roots
  for rejected TopK candidates, with conservative thresholds preserving rounded
  distance ties and primary-key order. No fast-math.
* Complete results up to 4096 bytes in memory; larger responses retain anonymous
  disk spools. Read JSONL through a fixed 64 KiB input buffer. Parse vector numbers
  without allocation where supported, retaining underflow fallback and the
  original double-to-float32 conversion.

The official protocol, driver, SQL and assertions are unchanged from
`f4d4e26653af823a7601668802f80e374ad6643c`. Commit durability and exact query
semantics remain covered by regression checks. ANN mode continues exact fallback.

## Final adjacent comparison

Every round uses 10000 rows, 128 dimensions, 1000 queries, K=10, half filtered to
25% of rows, and auto-committed batches of 100. Each item uses a fresh Linux volume
and cgroup, two CPUs, 128 MiB and no swap. Query memory/I/O include setup and ten
warmups; query time measures complete requests/responses after warmup. All IDs,
order, filters and distances matched independent Python truth. Both recall groups
were 1.0 in every round.

Versions ran next to each other; seed 7 reversed their order. No agent build or
test ran alongside timing. Other host work still caused large fluctuations;
Linux amd64 was emulated on Apple M4 Pro. These runs support query acceleration
but do not establish write improvement or predict native platform timings.

| Seed | Old write ms | New write ms | Old query ms | New query ms | Query time reduction |
| --- | ---: | ---: | ---: | ---: | ---: |
| 20261109 | 4217.38 | 5560.07 | 10403.67 | 6177.93 | 40.62% |
| 7 | 4823.27 | 1955.25 | 2787.16 | 2481.15 | 10.98% |
| 39034843 | 5641.50 | 7220.75 | 11170.11 | 4662.70 | 58.26% |

| Descriptive median | Old | New | Change |
| --- | ---: | ---: | ---: |
| Write time, ms | 4823.27 | 5560.07 | +15.28% |
| Query time, ms | 10403.67 | 4662.70 | -55.18% |
| Write peak memory, MiB | 20.105 | 22.246 | +10.65% |
| Query peak memory, MiB | 22.348 | 25.801 | +15.45% |
| Raw I/O per item, bytes | 7241728 | 7241728 | unchanged |

Memory is a real tradeoff: the 8 MiB vector/offset bound is supplemented by four
bits per cached row and bounded predicate keys. No lower I/O is claimed. Raw
cgroup counters lack certified dedicated-device/writeback attribution.
Per-metric medians describe runs; they are not combined into a synthetic best run.

[Old rounds](results/perf-paired-baseline.json),
[new rounds](results/perf-paired-candidate.json) and
[paired calculation](results/perf-paired-comparison.json) retain every T/B/M
value. Applying the local three-metric formula within each pair then taking the
median gives `0.547628`, versus `0.5` for unchanged performance. This proxy is
not the platform's `1968.948310`, a prediction of `5300`, or ranking points.

Earlier measurements of intermediate exact-v1 showed query medians of
2430.59 → 1073.18 ms, before predicate masks. Those are
[historical baseline](results/perf-5300-baseline.json) and
[intermediate results](results/perf-exact-v1.json), not final-source results.
[Two predicate probes](results/perf-predicate-probe.json) measured 269.12 →
181.44 ms and 235.13 → 210.92 ms over 200 queries. Separate final and old-image
rechecks are retained in `perf-final-benchmark.json` and
`perf-final-baseline-recheck.json`; their different time windows must not be used
to select favorable metrics. The latter corrects the tool's captured worktree
hash to the attested old-image source hash, preserving the original field.

## Rejected experiments

Storage append buffering, LTO and dimension-major blocks did not establish a
useful end-to-end gain. Consecutive-string and ASCII validation paths had
inconsistent results under host load. All were withdrawn.

The standalone in-house HNSW prototype required about 9.4 seconds and 136 million
distance evaluations to prepare Gaussian 10000×128D data. Its filtered traversal
cost and roughly 2 MiB graph memory did not establish an end-to-end benefit over
optimized exact search. Its raw harness log contains a UBSan alignment diagnostic;
an earlier sanitizer-clean description was incorrect. It is not integrated.
The [report](results/perf-ann-rejected.json) and
[restorable source diff](results/perf-ann-prototype.diff) preserve this limitation.
No result is attributed to a student decision.

## Final verification and identity

Core SHA256:
`b45723f072914196b18dfc9b79a4716a2a4422a728b65938097a645c1854648e`.
Tested Linux amd64 image `minivectordb:perf-final` has ID
`sha256:ae380805ffe66f2ace4db33d01e395bc9b9b2d5bd915c54c6da8ec4dae74097a`.

[Final commands and output](results/perf-final-tests.json) record:

* Unchanged official driver: 20/20 cases, 95 SQL, plus protocol boundaries.
* ASan/UBSan: public, protocol, 35 cache checks, 323 vector-number checks,
  five-seed differential regression, 375 vector checks and three 66002-row
  tree-split orders.
* Native sanitizer and Linux fault builds: 23 process kills, 36 recovery
  interruptions and 18 repeated restarts.
* [96 MiB, no-swap resource check](results/perf-final-resources.json): 40000 rows,
  142734352 data bytes before indexing, filtered queries, external sorting,
  large transactions, rollback, SIGKILL and restart passed.

[AVX2 evidence](results/perf-avx2-validation.json) verifies dispatch, actual SIMD
instruction execution and SQL with AVX2 on and off. It includes nonaligned data,
float32 extremes, subnormals, dimensions 1/4/8/17/128/256/1024 and exact primary-key
order for intentional ties. Tiny roundoff differences are documented.

The clean-export rebuild, executable comparison and final driver rerun are in
[the delivery report](results/perf-delivery.json). New platform performance
remains unmeasured. Learning evidence separates the user's actual goal from the
agent's engineering; it invents neither technical student replies nor higher scores.
