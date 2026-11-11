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
magic, length-prefixed row/posting blobs and 4096-byte B+ tree pages. Integers and
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

Publication order:

1. Flush all dirty new pages and set the data file length.
2. `fsync(data)`.
3. Write `catalog.next`, containing schema, all roots and committed EOF; verify
   local size bounds and include a checksum.
4. `fsync(catalog.next)`.
5. Atomically rename `catalog.next` to `catalog` — the unique visibility point.
6. `fsync(/data)` — publication is durable before success is acknowledged.

Errors before rename allow restoration of the statement savepoint. Errors after
rename terminate the process: the server must not report a failed statement then
continue from an older snapshot. Failure to deliver an already committed success
response is also fatal, leaving the outcome to recovery.

Recovery verifies catalog checksum/schema bounds, the data header and committed
tail. It truncates the unpublished tail and removes `catalog.next`, then reports
ready. Repeating any recovery step after SIGKILL yields the same published roots.
No index rebuild is necessary: row and physical vector roots were committed in
the same catalog. The covered failure model is process termination, as specified;
the suite does not simulate filesystem corruption or physical power loss.

Fault injection is compiled only into a dedicated test binary:
`MVDB_FAULT=<point>` and `MVDB_FAULT_HIT=<occurrence>` cause SIGKILL at named append,
sync, rename, response and recovery boundaries. Production builds ignore these
variables.

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
when projecting selected results. The double accumulation order is unchanged.
Larger or string orderings use approximately 4 MiB sorted runs and binary
levelled merges, bounding both active runs/file descriptors by O(log N).
Results are first materialized to an anonymous `/data` spool, so evaluation
failure cannot leak half of a successful result and large results do not stay in
heap memory. Spools are unlinked immediately and close through RAII.

CREATE VECTOR INDEX builds a second physical tree whose blobs contain the current
row offset and float32 vector. Every insert/update/delete maintains both roots in
the same statement. Unfiltered distance ordering can scan these narrow postings,
decoding only winning full rows. This is an exact physical access path; both exact
and ann retain precise recall. Filtered queries currently use primary-row scans.
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
spool experiment was withdrawn after its local weighted proxy regressed; results
continue to use the existing anonymous disk spool.
