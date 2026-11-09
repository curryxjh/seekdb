# Validation and performance record

Date: 2026-11-09, Asia/Shanghai. All results below are LOCAL; no formal platform
submission was attempted, and no official admission, E, rank or 80-point score is
claimed.

## Revision and environment

* Official baseline: `9f6df2ba5acf871376e22b64a3d0724e4b575044`.
* Final tested core commit: `4f4e0474e6eff1715625f6c9ec4a261c95623386`.
* Final `minivectordb/` SHA256: `998365883cb8276eafb1baf82ff1e862a5664d5c17c6cff29988f8d7b617ed75`.
* Initial implementation/benchmark: `f0448c9dc3f5fa6919e4fa7b84cdc1764e966c49`,
  source SHA256 `b096a3435c63c9b89591c7a87492cafd3414c5306b8a64e7c9da1f2a5ce64581`.
* Intermediate string-view optimization: `14607f3b6eb0353550d14712652f6d0cb144a6b1`,
  source SHA256 `b98ac89bb4d09ca4737bdbed80074ba62265a590406e6517922e93fce71280ef`.
* Host: Apple M4 Pro, macOS 15.7.4; Apple Clang 17, Python 3.14.6.
* Docker client/server 29.3.1/29.9.2; Linux x86_64 images emulated on arm64.
  Compiler: GCC 14.2.0, pinned image digest in Dockerfile. Compiler-image tests use
  its bundled Python. Production runtime: pinned Debian Bookworm slim.
* Source digest algorithm: sorted immediate files under `minivectordb/`, hashing
  each basename followed by its raw bytes. `tests/evidence.py` rechecks it.

## Correctness, crash and resource results

| Check | Actual result | Reproduction / evidence |
| --- | --- | --- |
| Original public v4 assertions | 20/20 cases, 95 SQL, unchanged expectations | `python3 tests/container_suite.py`; [output](results/container-public.txt) |
| Boundaries, framing, Unicode, float32, atomic statements and live index state | PASS | [production container regression](results/container-regression.txt) |
| Independent Python state model | 5 seeds x 150 steps, state and TopK compared after every step | seeds 7, 42, 20261109, 8675309, 39034843; regression output |
| ASan + UBSan | public, regression, crash and vector sweep PASS, no diagnostics | `SANITIZE=1 FAULTS=1 OUTPUT=build/minivectordb-sanitize ./build.sh`; [public](results/sanitize-public.txt), [regression](results/sanitize-regression.txt), [crash](results/sanitize-crash.txt), [sweep](results/sanitize-vector-sweep.txt) |
| Linux x86_64 fault build | public, differential and all crash scenarios PASS | `python3 tests/linux_suite.py`; [output](results/linux-fault-suite.txt) |
| Deterministic process failure | 23 kill scenarios, 36 recovery interruptions and 18 repeated clean restarts | includes commit old/new uncertainty, acknowledged commit, multi-table atomicity, auto-commit, mid-row/posting append and index publication |
| Dimension / K / selectivity / mutations | 375 ordered ID/distance checks PASS | held-out seed 14223819; dimensions 1/17/256; K 0/1/10/63/>N; empty, roughly 1%, 25%, 100% filters; scan, index, own writes, rollback, restart; [output](results/container-vector-sweep.txt) |
| Above-memory execution | PASS: 40000 rows, 143434768 bytes before index, 96 MiB limit, swap 0 | `python3 tests/container_checks.py`; [raw report](results/container-resources.json) |

The resource test includes a single large transaction, committed data larger than
memory, index construction, filtered TopK, spill/merge string sorting, large
UPDATE/DELETE and rollback, committed deletion, uncommitted deletion followed by
Docker SIGKILL, and restart. Cgroup memory.peak reached the 96 MiB limit as file
cache was reclaimed; the process survived and all assertions passed. This is a
local resource condition, not a frozen organizer limit.

Every production-image test ran in the foreground, without networking, with all
persistent/temporary DB files in `/data` and swap disabled. The fault compiler
image uses a separate 512 MiB test limit. Process-kill tests do not establish
physical power-loss or arbitrary-corruption guarantees.

## Measured optimization sequence

The first physical-posting experiment improved query T by only about 3.2% while
adding disk and memory. Source inspection then found successful `read_bytes`
checks allocating long diagnostic strings for each float. Changing `require` to
accept string views defers construction until error, preserving error semantics.
Bulk vector decoding next validates the complete span once and loads little-endian
float bits directly, preserving double accumulation and Euclidean output. Two
parser boundary fixes in that revision do not affect the valid benchmark SQL.
No approximate recall relaxation, unsafe math flags, deferred commit sync or
sample-answer special cases were used.

The default access path remains exact scan. A physical vector index is only
created on explicit SQL request; it is retained to implement the required index
lifecycle. Final indexed queries are about 12.7% faster than final scan here, at
extra resource cost. This does not establish a better combined official score.

All six configurations below used three complete rounds, seeds 20261109, 7 and
39034843. Medians are descriptive per-metric summaries, not a synthetic best-round
score. Each JSON retains all paired per-round T/M/raw-IO values.

| Configuration | Median write T (ms) | Median query T (ms) | Median query M (MiB) | Raw rounds |
| --- | ---: | ---: | ---: | --- |
| Initial scan | 1745.367 | 22683.473 | 21.383 | [benchmark-scan.json](results/benchmark-scan.json) |
| Initial postings | 1589.325 | 21955.652 | 28.871 | [benchmark-postings.json](results/benchmark-postings.json) |
| String-view checks / scan | 1587.840 | 5375.055 | 21.637 | [benchmark-checks-scan.json](results/benchmark-checks-scan.json) |
| String-view checks / postings | 1615.107 | 5093.851 | 27.090 | [benchmark-checks-postings.json](results/benchmark-checks-postings.json) |
| Final scan | 1584.587 | 1922.141 | 21.340 | [benchmark-final-scan.json](results/benchmark-final-scan.json) |
| Final postings | 1584.151 | 1677.221 | 26.367 | [benchmark-final-postings.json](results/benchmark-final-postings.json) |

Final paired rounds:

| Configuration | Seed | Write T (ms) | Query T (ms) | Query M (MiB) | Index prepare (ms) | Recall unfiltered / filtered |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Final scan | 20261109 | 1584.587 | 1915.057 | 21.340 | 0.001 | 1.0 / 1.0 |
| Final scan | 7 | 1573.270 | 1925.631 | 23.637 | 0.001 | 1.0 / 1.0 |
| Final scan | 39034843 | 1588.479 | 1922.141 | 21.117 | 0.000 | 1.0 / 1.0 |
| Final postings | 20261109 | 1584.145 | 1631.272 | 26.367 | 21.259 | 1.0 / 1.0 |
| Final postings | 7 | 1584.151 | 1718.656 | 26.316 | 20.646 | 1.0 / 1.0 |
| Final postings | 39034843 | 1600.059 | 1677.221 | 28.777 | 21.077 | 1.0 / 1.0 |

Workload: 10000 rows, 128 dimensions, 1000 queries, K=10; half the queries filter
one ordinary group (25% selectivity), batches of 100 rows auto-commit with fsync.
Each write/query item starts in a fresh Linux named volume/cgroup, 2 CPUs,
128 MiB, no swap, network none. Query resource counts include startup, loading,
index build and ten warmups; only query T excludes those preparation steps.
T includes the complete request/response and JSON parsing at the host client.
Stored vectors and scalar columns were verified after write-item restart.
Independent Python float32 + math.dist generates truth outside the DB container;
all query IDs, counts, order, filters and distances were checked.

Raw query io.stat in all final rounds reports 0 read bytes and 7397376 write bytes
for scan, versus 12972032 write bytes with postings. Host cache was not evicted.
This counter is retained honestly, but dedicated-device and deferred-writeback
attribution are unverified; it cannot be substituted for certified organizer B.
There is no organizer baseline, so no official E is calculated. The measured
scan median improved 11.80x from initial to final in this local environment.

## Failures retained and corrected

* Docker Desktop/macOS bind mounts allowed two separate containers to acquire
  what should have been the same directory lock. Same-container and Linux-volume
  controls both rejected the second process. The harness now uses Linux named
  volumes without skipping the lock assertion. [Original failure/control](results/container-initial-failure.txt).
* The initial parser accepted `+-1`, duplicate PRIMARY KEY declarations and an
  empty quoted identifier. Focused old-image/final-image comparison preserved
  the actual responses, and regression tests now reject all three.
  [Before/after](results/parser-corrections.json).
* Unimpressive initial index resource tradeoff is preserved in the raw rounds;
  index creation was not made automatic and no official gain is asserted.

## Minimal-diff and delivery audit

No storage/ANN/parser dependency or third-party default configuration was added.
C++20 and warning flags define the build contract. The only build switches enable
sanitizers and deterministic testing faults; production has neither fault switch
nor runtime fault control. The public SQL surface determines transaction, type,
filtering and index branches. Cache/run/heap/request limits bound resources; none
is claimed as an organizer default. The bulk decoder adds no architecture-specific
or approximate fast path. Tests live in `tests/`, not `tools/`.

Source, Dockerfile, tests, documentation and the sole authentic
`agent/conversation.json` are delivered together. The original empty root JSON
placeholder is removed. The original master worktree and its untracked files
were preserved. Git delivery uses `minivectordb-round2` as explicitly authorized,
without force push. Exported-commit rebuild and evidence validation are recorded
in `results/delivery-check.json`.

## Remaining limits and target gap

Formal stdin/stdout framing, ready/error/mode contract, resource/time limits,
certified I/O baseline and complete 29-case admission still require the organizer
package/platform. The adapter is LOCAL. ann currently has exact fallback rather
than sublinear approximate search. Append/COW storage has no compaction and
accumulates unreachable data/tombstones. Largest-request memory budgets still
need checking against frozen limits. Cross-container macOS bind-mount flock is
not supported in the observed environment; Linux volumes were verified.

The learning artifact includes actual user authorization and engineering/test
results. There is no user technical prediction, explanation, correction or
transfer feedback in this session. `learning_units` is empty rather than forged;
the Agent's work is not assigned to a student. Neither dual first places nor
80 points has been established, and formal submission remains unattempted.
