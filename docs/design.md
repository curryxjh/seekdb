# MiniVectorDB design

## Boundaries

`protocol.*` owns bounded JSONL request parsing and exact ID preservation. `sql.*` owns UTF-8 validation, lexing, parsing and literals.
`storage.*` owns disk records, a COW B+ tree, bounded page cache and durable root
publication. `database.*` owns schema/type binding, transaction roots, row/index
maintenance, filtering, selection and external sorting. `main.cpp` connects the
official JSONL transport to the database.

There are no third-party execution libraries, downloaded runtime artifacts,
network services, hidden sample-answer dispatches or full-database heap maps.

## Persistent state and visibility

`/data/LOCK` uses nonblocking exclusive `flock`. `/data/data` contains a 16-byte
magic, length-prefixed row/posting blobs, 4096-byte B+ tree pages and commit records. Integers and
float bits are encoded little-endian explicitly. A tree leaf maps signed int64
primary keys to row offsets; zero references are deletion tombstones. Inner
entries store each child's minimum key. Splits propagate updated minimum keys.
No leaf links can point into a different snapshot.

On overflow caused by appending a new rightmost entry, the left page retains
255 entries and the new right sibling starts with one. This applies to leaves
and internal child entries; middle insertions retain balanced splits. Appending
is detected before insertion, and replacement/tombstone updates do not trigger
this policy. There is no minimum-occupancy assumption in lookup, scan or recovery.
This reduces historical half-empty pages for ascending keys without changing
the statement savepoint or publication boundaries.

The page cache holds at most 512 pages with LRU eviction, flushing dirty pages on
eviction. Disk reads use a read-only mapping; pages remain reclaimable by the OS,
and Linux cgroup accounting includes that file cache. Tree scans retain one node
per level (maximum accepted depth 32). There is no heap structure proportional
to record count.

The in-memory catalog holds schema and primary/vector roots, bounded by a 4 MiB
serialized catalog and local schema limits. At a statement's start, save roots
and the logical EOF. Pages older than this boundary are immutable. A mutation
copies only the path it changes; pages newly allocated by that statement may be
updated in place. Consequently a failed statement can restore its roots and
truncate everything at or beyond its EOF. Earlier successful statements in the
same explicit transaction remain available. Scans for UPDATE/DELETE use the old
root, while writes produce a new root, avoiding duplicate/omitted iteration.

BEGIN retains the committed roots; reads use current roots, including own writes.
ROLLBACK restores the committed catalog and EOF. Auto-commit publishes exactly
one statement. DDL and index construction participate in the same mechanism.
The catalog publishes all table and index roots together, so no table can expose
a partially committed multi-table transaction.

## Durable commit and repeatable recovery

The current writer uses append-only commit records in the data file. COW page
immutability and per-statement savepoints are unchanged. Publication order:

1. Flush dirty new pages and set the data file length.
2. Hash the bytes appended since the previous published EOF in bounded 64 KiB
   chunks. Append the complete catalog (schema, roots and new committed EOF).
3. Append a 56-byte footer containing catalog offset/length/checksum, previous
   published EOF, payload checksum and the 16-byte commit marker.
4. `fsync(data)` persists data, catalog and footer together before acknowledgement.

A checksum over both the catalog and newly appended payload prevents accepting a
footer whose preceding writes are incomplete. Earlier committed pages are never
modified. Once footer writing begins, any I/O failure terminates the process;
it cannot report failure and continue from stale roots. Missing responses remain
outcome-uncertain. Early writeback still never publishes a transaction.

Recovery searches backward in overlapping 1 MiB chunks, validates candidate
footers, catalog/payload checksums, schema bounds and the original data header,
then truncates the uncommitted tail to the selected EOF. Memory used to search
or hash is bounded independently of tail size. No index rebuild is required.
The process-kill suites, torn-footer and damaged-payload tests validate this
implementation; they do not simulate physical power loss on real hardware.

A baseline directory with the legacy external catalog is readable and migrates
on its first write. After a durable journal commit, a format guard replaces the
old external catalog once and is synced along with its directory entry. This
makes old writers refuse the upgraded directory instead of truncating it to an
old snapshot. Subsequent commits need only the single data-file sync. New-directory
initialization persists directory entries before ready. Downgrading requires a
backup made before migration; there is no reverse-format converter.

Fault injection is compiled only in test builds. New points
`after_commit_catalog` and `after_commit_footer` supplement append, writeback,
response and recovery points. Former `after_catalog_*` and
`after_directory_sync` names remain compatibility aliases around journal
publication; they no longer imply a rename on every commit.

## Types, execution and exact search

The parser validates an entire statement before execution. Multirow INSERT still
validates and writes rows incrementally; a late duplicate/type failure restores
the savepoint. BIGINT uses checked decimal int64 conversion. VARCHAR validates
Unicode scalar encodings, counts codepoints rather than bytes and compares valid
UTF-8 lexicographically. Predicates do not apply the column's write length limit
to comparison constants.

VECTOR converts input to float32, validates finiteness/dimension and stores its
bits. L2 accumulates double differences and returns the square root. Scalar
filtering and snapshot visibility precede candidate selection. All orderings break
ties by ascending primary key, including descending scalar/distance order.

Primary-key equality uses a tree lookup. Other queries scan current primary
roots. Numeric/distance ORDER BY with K <= 16384 uses bounded TopK selection.
The primary-row scan reuses one array of borrowed field spans. It checks complete
row boundaries, compares scalar predicates without owning string allocations,
and computes distance directly from serialized float32 fields. Spans are used
inside the current callback before another storage access could remap the file.
Sort keys own any strings that outlive a callback; full rows are decoded only
when projecting selected results. Distance kernels use double accumulation; the
current SIMD grouping and roundoff checks are described below.
Larger or string orderings use approximately 4 MiB sorted runs and binary
levelled merges, bounding both active runs/file descriptors by O(log N).
Results complete in a bounded memory buffer or an anonymous `/data` spool, so
evaluation failure cannot leak half of a successful result and large results do
not stay in heap memory. Disk spools are unlinked immediately and close through RAII.

CREATE VECTOR INDEX builds a second physical tree whose blobs contain the current
row offset and float32 vector. Every insert/update/delete maintains both roots in
the same statement. Unfiltered distance ordering can scan these narrow postings,
decoding only winning full rows. This is an exact physical access path; both exact
and ann retain precise recall. Bounded TopK queries use the vector-prefix cache
described below, including predicates; other filtered queries use primary rows.
There is no sublinear approximate-search claim.

## Resource bounds and tradeoffs

The page cache, run buffer, bounded heap, schema limits and request limit bound
heap consumers independently of database cardinality. One row and one request's
AST can still be large; the exact frozen organizer limits must be checked before
formal admission. An unlimited result is disk-backed and still requires output
time and disk space.

The append/COW design simplifies statement rollback and recovery but does not yet
compact unreachable old rows/pages or remove tombstone keys. Repeated mutations
can therefore consume substantial extra disk and scan time. A future compactor
would need its own atomic generation publication, crash tests and measured
resource budget; it is not silently run outside the measurement window.

Memory/IO counters in local reports are container cgroup counters. `io.stat`
device attribution and complete deferred writeback attribution have not been
certified against an organizer data device. No official E is computed from them.
`tests/compare.py` pairs equal seeds and workloads, checks correctness/recall and
local hard limits, applies all three weights within each round, then takes the
median. Per-metric medians are shown only for description. An 8 KiB in-memory
spool experiment was withdrawn after its local weighted proxy regressed. The
2026-11-12 iteration below re-evaluates a smaller buffer with the new query path;
the older result remains historical evidence.

## Current bounded query acceleration

The raw vector/offset prefix uses at most one eighth of the detected memory
limit, capped at 128 MiB. The screening-data cap is half that prefix budget.
The reader considers cgroup-v2 `memory.max` both at the visible root and along
the current process's cgroup ancestors, plus the conventional v1 memory-limit
file. If no finite limit is visible, the raw prefix defaults to 128 MiB.
Four predicate masks and temporary candidate arrays remain proportional to
this bounded prefix. Larger tables stream their tail exactly.

Mutations, failed statements, table switches and rollback release vector, row,
mask and screening allocations before offsets can be reused. This prevents a
large previous SELECT from retaining its cache during a large subsequent write.

On CPUs supporting AVX512F/BW/VNNI, four independent integer dot products share
query loads. Adding 128 to signed row bytes is corrected by `256*sum(query)`
in the squared-distance formula; no lossy arithmetic is introduced. Masked tail
loads cover arbitrary dimensions without reading past their spans. All
intermediates fit int32 at the existing maximum screened dimension of 4096.
AVX2, ARM and scalar paths remain available on other machines. Conservative
quantization bounds and original float32 refinement still determine exact TopK.

## Historical 2026-11-12 bounded query acceleration

Exact L2 uses four independent double accumulators; x86_64 dispatches to an AVX2
implementation only if CPUID and OS support permit it. There is no fast-math or
float32 subtraction/sum overflow. Only likely heap winners need a square root;
a conservative nextafter threshold preserves rounded-distance ties. Projection
still computes its own expression, including when it differs from ORDER BY.

One table's vector prefix and row offsets occupy at most 8 MiB of vector/row
capacity. Larger tables stream the remaining primary-key tail on every query.
Four packed predicate masks retain at most four bits per cached row, with keys
bounded to 4096 bytes each. Long predicates stream normally. Every mutation,
including a failed statement, invalidates vectors and masks before offsets can
be reused. Rollback also invalidates; committed immutable roots may be reused.
Tables/dimensions release old vector capacities before reserving new buffers.
This is a data cache, not a stored-answer or benchmark-specific cache.

The minimal-diff audit retains only branches that enforce these bounds, select
supported CPU instructions, preserve rollback visibility or handle the official
input/result boundaries. The 8 MiB prefix, four masks, 4096-byte keys/results and
64 KiB input buffer are local resource choices, not organizer defaults. No runtime
configuration option, dependency, build flag or approximate-query mode was added.

Results <=4096 bytes complete in memory; larger responses spill to an unlinked
file under the data directory. Errors cannot emit partial successful responses.
Input uses a fixed 64 KiB buffer while preserving JSONL request boundaries,
fragmented writes, surplus input, shutdown behavior and per-response flushing.
The immutable approximate graph experiment was not integrated: construction and
filtered traversal costs outweighed its query benefit; its unfavorable records
and sanitizer caveat are retained separately. ANN mode keeps exact fallback.

## 2026-11-13: exact integer screening and bounded dirty pages

The vector prefix remains bounded to 8 MiB, including row offsets. `screen.*` adds
at most 3 MiB of signed int8 codes, integer norms and reconstruction-error bounds,
for dimensions 32–4096. Larger dimensions and uncached tails keep exact streaming.
Mutations, failed statements and rollback invalidate both representations before
root offsets can be reused. No query answers are cached.

For scale s, reconstructed vectors x' and q' satisfy
`abs(||x-q|| - ||x'-q'||) <= e_x + e_q`. Bounds include reconstruction and
arithmetic rounding. If E bounds every row's error plus query error, the Kth
integer candidate distance provides an exact-safe cutoff with a 2E margin.
Strict rejection and an integer rounding guard preserve boundary candidates;
remaining records use the original stored float32 vectors and compensated double
accumulation. Sparse predicate masks are traversed by set bits before distances.
The prefix's cutoff is also safe when an uncached tail is subsequently scanned:
adding records can only improve the final cutoff.

AVX2 uses abs/sign plus maddubs/madd on [-127,127] codes. Excluding -128 avoids
sign overflow; pair products are at most 32258, so int16 saturation cannot occur.
At 4096 dimensions, squared integer distances remain below INT32_MAX. ISA
dispatch happens once for a batch. ARM dot-product builds use four independent
accumulators; scalar execution remains available on other CPUs.

ASCII/quoted-string fast paths retain UTF-8, escape, raw-control and complete-token
validation. Floating from_chars supplies decimal syntax validation on successful
full consumption; fallback parsing still checks the explicit grammar, preserves
double-to-float32 rounding, and rejects hexadecimal/non-finite/malformed values.
Distance projections reuse the ordering key only for identical bound expressions.
Ordinary scalar projections borrow row fields; vector projections still decode
vectors. No fast-math or relaxed transaction semantics are introduced.

Data and temporary-file dirty pages require bounds independent of heap caches.
Data-file growth is synchronized at 8 MiB intervals without publishing catalog
roots. Spilled results/runs are drained in 8 MiB chunks; a retained sorted run is
finished and synchronized before it is stored for a later merge. Thus dirty tails
do not accumulate with merge depth. Small in-memory TopK responses avoid this I/O.

Early data synchronization is not a commit: only the existing catalog publication
protocol changes visible durable roots. Recovery truncates unpublished data to the
last committed end. Fault point `after_early_writeback` proves a kill after early
synchronization still exposes only committed records. Publication still synchronizes
all final data, catalog and directory before acknowledgement.
