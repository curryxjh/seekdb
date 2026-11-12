# Current verification status

Formal admission passed for b3b16af1e8429b3daed14bbdfedc89bf67c6e2a8 in the user's
2026-11-11 19:58 submission. Platform raw performance: 1968.948310; raw Agent: 1.
The 2026-11-12 optimized source has not been formally submitted. Current test and
performance evidence is recorded in [performance-iteration.md](performance-iteration.md)
and `results/perf-final-*.json`. The historical evidence below retains its original
source associations.

# Validation and performance record

Date: 2026-11-11, Asia/Shanghai. Historical performance results below are LOCAL; the protocol checks use the official released driver.
The user subsequently reported a formal platform failure at 21:54 for delivered
commit `3ccc14effd6501ffb10d5e6aaa0ba2853032cb86`. Authenticated inspection
confirmed failed admission; performance and prompt evaluation were skipped.
No detailed failure log was returned. See [platform investigation](platform-failure.md).
No official admission, E, rank or 80-point score is claimed.

## Revision and environment

* Initial official baseline: `9f6df2ba5acf871376e22b64a3d0724e4b575044`.
* Official protocol/driver update: `f4d4e26653af823a7601668802f80e374ad6643c`.
* Final tested core before protocol repair: `cf294bbc8a664988ef11b5884d35ae57c1dd12cb`.
* Pre-repair `minivectordb/` SHA256: `ef7e8244c6e5b2dad5bd66440905fb56db6d452291b7a94114cb975f70128248`.
* Repaired `minivectordb/` SHA256: `1399594fad6d6684dd91517c3556d11961d7598a1134176a392f335ae8364492`.
* Previous pushed delivery: `c8423c1c5c3fa15d8b5dc0d131aec81d35d425d9`;
  its core was `4f4e0474e6eff1715625f6c9ec4a261c95623386`.
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

## Official protocol repair

The unchanged official driver rejected the old delivery at the startup handshake:
`expected ready, got {'ready': True, 'protocol': 'minivectordb-local-v1'}`.
The corrected program implements the exact ready object, JSONL execute/shutdown,
typed request IDs, nested errors and per-response flush. The old SQL stream
framer and local mode command were removed. SQL execution and storage semantics
were preserved; official SQL cases, assertions, protocol and driver match upstream.

The native and Linux amd64 production builds pass all 20 public cases / 95 SQL
under that driver. Additional checks cover ID types and precision, strict JSON,
Unicode escapes, fragmented input, continued execution after errors, shutdown
rollback and restart. ASan/UBSan public, protocol, differential, crash and vector
suites pass. The repaired image also passes the 40000-row test under 96 MiB with
swap disabled. See [old handshake](results/protocol-before/result.json),
[official Docker cases](results/protocol-docker-public/result.json),
[protocol and resource commands](results/protocol-docker-tests.json) and
[sanitizer commands](results/protocol-sanitize-tests.json).

A clean staged-tree export was rebuilt offline for Linux amd64, then passed the
unchanged official 20-case driver and protocol boundary suite again. The rebuilt
database executable has the same SHA256 as the tested production image; image
config IDs differ and both identities are preserved in
[the delivery report](results/protocol-delivery.json).

The performance numbers and broad split/fault results below describe the
pre-repair revisions explicitly named in their reports. They are retained as
history, not measurements of the new transport. The user subsequently submitted
b3b16af and passed formal admission, as recorded at the top of this document.

## Pre-repair correctness, crash and resource results

| Check | Actual result | Reproduction / evidence |
| --- | --- | --- |
| Original public v4 assertions | 20/20 cases, 95 SQL, unchanged expectations | `python3 tests/container_suite.py`; [output](results/opt-final-production-suite.txt) |
| Boundaries, framing, Unicode, float32, atomic statements and live index state | PASS | [production container regression](results/opt-final-production-suite.txt) |
| Independent Python state model | 5 seeds x 150 steps, state and TopK compared after every step | seeds 7, 42, 20261109, 8675309, 39034843; regression output |
| ASan + UBSan | public, regression, crash and vector sweep PASS, no diagnostics | `SANITIZE=1 FAULTS=1 OUTPUT=build/minivectordb-sanitize ./build.sh`; [public](results/opt-final-sanitize-public.txt), [regression](results/opt-final-sanitize-regression.txt), [crash](results/opt-final-sanitize-crash.txt), [sweep](results/opt-final-sanitize-vector_sweep.txt) |
| Linux x86_64 fault build | public, differential and all crash scenarios PASS | `python3 tests/linux_suite.py`; [output](results/opt-final-linux-fault.txt) |
| Deterministic process failure | 23 kill scenarios, 36 recovery interruptions and 18 repeated clean restarts | includes commit old/new uncertainty, acknowledged commit, multi-table atomicity, auto-commit, mid-row/posting append and index publication |
| Dimension / K / selectivity / mutations | 375 ordered ID/distance checks PASS | held-out seed 14223819; dimensions 1/17/256; K 0/1/10/63/>N; empty, roughly 1%, 25%, 100% filters; scan, index, own writes, rollback, restart; [output](results/opt-final-production-sweep.txt) |
| Multi-level tree splits / cache eviction | PASS: 66002 rows each, ascending / descending / shuffled; seed 30274159 | native, sanitizer, Linux fault build and production; [sanitizer output](results/opt-final-sanitize-storage_splits.txt) |
| Above-memory execution | PASS: 40000 rows, 142734352 bytes before index, 96 MiB limit, swap 0 | `python3 tests/container_checks.py`; [raw report](results/opt-final-resources.json) |

All final-source commands, exit statuses, source revision, elapsed times and log
hashes are recorded in [opt-final-tests.json](results/opt-final-tests.json). Earlier
logs remain historical; none is relabeled as a final-source execution.

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

## Further optimization requested by the user

The user explicitly requested further optimization across time, physical I/O and
peak memory, together with better authentic prompt evidence. This is real task
steering; it is not a fabricated technical explanation or learning score.

Experiments were isolated in exported commits, with the image built from those
same sources. No CPU-heavy tests or builds ran alongside timed benchmarks. Each
configuration used three seeds (20261109, 7, 39034843), the same 10000-row / 128-dim
/ 1000-query workload and cgroup settings described below. All query assertions
passed, and filtered/unfiltered recall was 1.0 in every round.

| Configuration | Core revision | Write T median (ms) | Query T median (ms) | Write M median (MiB) | Query M median (MiB) | Query raw B (bytes) |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Previous code, remeasured | c8423c1 | 1558.102 | 1803.682 | 23.918 | 23.270 | 7397376 |
| Borrowed row fields | ea95bee | 1559.606 | 1430.287 | 22.820 | 23.379 | 7397376 |
| Small spool buffer, withdrawn | 3a39bbf | 1571.857 | 1593.832 | 23.000 | 23.250 | 7397376 |
| Borrowed fields + packed append splits, retained | cf294bb | 1560.221 | 1412.452 | 20.844 | 23.188 | 7241728 |
| Final explicit postings | cf294bb | 1559.543 | 1363.867 | 20.902 | 28.004 | 12660736 |

Raw rounds: [previous](results/opt-previous-scan.json),
[borrowed](results/opt-views-scan.json), [spool](results/opt-spool-scan.json),
[final scan](results/opt-packed-scan.json),
[final postings](results/opt-packed-postings.json). These files bind each source
hash and Docker image. The previous-code rerun is the appropriate comparison for
this iteration; earlier historical measurements varied with host conditions.

Default scan query time decreased 21.69% relative to the previous-code rerun;
write time changed by +0.14%. Raw I/O decreased 2.10% in both phases. Write memory
median decreased 12.85%; query memory changed by only -0.35%, which is too small
and noisy to establish a reliable query-memory improvement. None of these
three-round measurements establishes statistical significance on native hardware.

The borrowed-field hypothesis was that repeated owning string/vector allocations
waste work for rejected candidates. The scan now validates row spans once, filters
and calculates distance from borrowed bytes, and decodes only projected winners.
The focused regression uses variable Unicode strings, a vector between scalar
columns, a nonleading primary key, distinct ORDER BY/projection query vectors,
ASC/DESC order, ties and LIMIT.

The spool hypothesis was that avoiding temporary files for results <=8 KiB would
improve latency/I/O. It did not: measured query time rose, and raw I/O stayed the
same. The code was withdrawn in c07d3aa; its commit, all three rounds and unfavorable
comparison remain available. The result is local evidence, not a claim that this
optimization is universally harmful.

The split hypothesis was that ascending insertion leaves half-empty historical
pages. Newly appended rightmost entries now split 255/1; middle insertion keeps
the balanced split. A separate 66002-row test for each of ascending, descending
and shuffled signed keys exceeds 255x255 fanout. It verifies point lookups,
complete ordered contents, eviction, rollback, late duplicate failure, preservation
of an earlier successful statement, SIGKILL of uncommitted inserts and repeated
restart. Seed 30274159 was held out from benchmark tuning.

`tests/compare.py` checks matching workload/environment/seeds, correctness,
recall and hard limits, computes 0.6/0.2/0.2 time/I/O/memory terms within each
round, applies twice the query weight, then takes the median. It never combines
the best individual metrics from different rounds. Epsilons are 1 ms / 1 MiB /
1 MiB. Its output is explicitly a LOCAL proxy; raw device/writeback attribution
and the organizer baseline remain unavailable.

| Paired local comparison | Median proxy (unchanged = 0.5) | Decision / evidence |
| --- | ---: | --- |
| Previous rerun → borrowed fields | 0.522306 | retain; [all rounds](results/opt-views-comparison.json) |
| Borrowed fields → spool buffer | 0.494401 | withdraw; [all rounds](results/opt-spool-comparison.json) |
| Borrowed fields → packed splits | 0.504356 | retain; [all rounds](results/opt-packed-comparison.json) |
| Previous rerun → final scan | 0.531296 | [all rounds](results/opt-final-comparison.json) |
| Previous historical postings → final postings | 0.523674 | separate historical comparison; [all rounds](results/opt-postings-comparison.json) |

These values are not official E or ranking points. Explicit indexing remains a
resource tradeoff: query time is only 3.44% below the new scan median, while raw
query I/O is 74.83% higher and memory median is 20.77% higher. It is therefore not
created automatically. The source still performs exact search and retains durable
commit sync, strict filters, ordering and float32 semantics.

## Earlier optimization sequence (historical delivery)

The following six configurations retain the previous delivery measurements and
source revisions. Files named `benchmark-final-*` describe that earlier core,
not the final code of the new iteration above.

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

## Historical minimal-diff and delivery audit

No storage/ANN/parser dependency or third-party default configuration was added.
C++20 and warning flags define the build contract. The only build switches enable
sanitizers and deterministic testing faults; production has neither fault switch
nor runtime fault control. The public SQL surface determines transaction, type,
filtering and index branches. Cache/run/heap/request limits bound resources; none
is claimed as an organizer default. The bulk decoder adds no architecture-specific
or approximate fast path. The new RowView type branches follow existing SQL
types; the two append-detection branches distinguish new end entries from
replacement/middle entries. They add no configuration knobs. The unhelpful
spool branch and its 8 KiB setting were removed. The comparison script options
only select input/output evidence files; they do not affect database execution. Tests live in `tests/`, not `tools/`.

Source, Dockerfile, tests, documentation and the sole authentic
`agent/conversation.json` are delivered together. The empty upstream root JSON
placeholder is retained; the sole learning artifact remains under `agent/`.
The original master worktree and its untracked files
were preserved. Git delivery uses `minivectordb-round2` as explicitly authorized,
without force push. The pre-repair exported-commit rebuild and evidence validation are recorded
in `results/delivery-check.json`.

## Remaining limits and target gap

The released JSONL stdin/stdout contract is now implemented and verified with the
unchanged official driver: 20/20 public cases and 95 SQL pass on native and
linux/amd64 Docker builds. The user subsequently passed formal admission on
b3b16af; this optimized revision has only local verification. Official ranking
baselines and the new platform result remain unavailable. `ann` currently has exact fallback rather than sublinear
approximate search. Append/COW storage has no compaction and accumulates unreachable
data/tombstones. Cross-container macOS bind-mount flock is not supported in the
observed environment; Linux volumes were verified.

The learning artifact includes actual user authorization and engineering/test
results. The user's later platform-failure report is retained as an actual
independent challenge to the delivery evidence. It has no assigned score or
invented technical explanation, prediction, transfer response or completed
repair. The Agent's work is not assigned to a student. Neither dual first
places nor 80 points has been established. The latest observed formal admission
passed at b3b16af, with raw performance 1968.948310. The optimized revision has
not been formally submitted; the agent did not submit or retry it.
