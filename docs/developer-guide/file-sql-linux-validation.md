# Competition Linux validation

The uncommitted file SQL implementation on top of `4111d160a` was validated on
2026-10-29. That merge includes `upstream/master` at `95db14417` and retains the
FTS optimization from `6381846afd3d9a553512b8792d7d98f58346b7e9`.

## Environment

- AlmaLinux 8, Linux aarch64, Docker container on an ARM64 host.
- Container memory: approximately 8 GiB; build concurrency: 2.
- Repository SDK Clang 17.0.6 and the repository's Bazel/build.py toolchain.
- Dedicated seekdb instance: port 12991, memory budget 2 GiB, data file 1 GiB,
  log disk 2 GiB.
- Integration fixture generation: Python 3.9, PyMySQL 1.2.3, PyArrow 17.0.0.
  PyArrow generates fixtures only; all queries execute in seekdb.

The database and tests ran inside Linux. No native macOS test result is included.
The x86-64 workflow is supplied separately and has not been run on GitHub.

The tested server binary SHA-256 was
`cafabc51a15c47c79cc997d1e057d8334e03a1f8ca6c3a6b4614773b44ede30a`.

## Results

| Check | Result |
| --- | --- |
| `python3 build.py build -j2 seekdb` | Passed |
| Bazel `test_file_sql_parser` | 3/3 cases passed |
| Bazel `test_file_scan_utils` | 20/20 cases passed |
| Standalone reader tests with AddressSanitizer and UndefinedBehaviorSanitizer | 20/20 cases passed |
| `file_sql_regression.py` against the final binary | 17/17 cases passed |
| `tokenize_regression.py` against the final binary | 1829 complete-result checks passed |
| Unmodified full FTS benchmark and scoring script | Completed; reference score 100/100 |

The final FTS result digest was
`a92f47b9c2a9cc6818e7ae09508f6a8df6ede7aceea0f74eca76197af7b33f4b`.

SQL coverage includes CSV quoting, CRLF, BOM, NULL versus empty string,
full-file schema inference, JSONL unions and conflicts, Parquet V1/V2 and
unsigned boundaries, native aggregates and joins, rescan, feature toggles,
metadata changes, path restrictions, malformed inputs and COPY round trips.
COPY cases also cover automatic SQL parameterization of format options,
batch execution over ordinary tables, no-overwrite publication and cleanup
after a query fails.

Linux validation exposed and fixed missing Bazel header dependencies, COPY
destination and option literals becoming SQL parameters, and zero-length
allocation for an empty NULL-text option.

## Full FTS benchmark

The original benchmark, generated data, SQL, timer, scorer and baseline were
used without modification. Parameters were `ROWS=20000`, `BATCH=500`,
`ROUNDS=3000`, `QUERY_ROUNDS=200`, `SAMPLES=3`, `WARMUP=30`, `SKIP_LOAD=0`.
The report label was `linux-aarch64-final`, with `git_dirty=1`.

| Scored metric | Measured time |
| --- | ---: |
| `build_ik_all_sec` | 4.363 s |
| `build_ik_content_sec` | 3.252 s |
| `build_beng_en_sec` | 1.837 s |
| `tokenize_ik_avg_ms` | 0.1098 ms |
| `tokenize_beng_avg_ms` | 0.0939 ms |
| `query_cn_avg_ms` | 0.9266 ms |
| `query_beng_avg_ms` | 1.3465 ms |
| `query_mixed_avg_ms` | 1.0798 ms |
| `query_limit_avg_ms` | 1.1944 ms |

The scorer compared these measurements with its fixed baseline and returned
100/100. This container differs from the official evaluation environment;
that score is a reference, and its improvement percentage is not a measured
before/after speedup on the same hardware. Official scoring still requires
the official Linux environment.

Reproduction commands and the manually dispatched Ubuntu x86-64 workflow are
documented in [file-sql.md](file-sql.md).
