# Querying local files

File tables use the native seekdb resolver, optimizer and executor. The feature
is disabled by default. Enable it for the session and configure an allowed local
directory before querying files:

```sql
SET GLOBAL secure_file_priv = '/srv/seekdb/files';
SET SESSION enable_file_sql = ON;

SELECT id, name FROM read_csv('/srv/seekdb/files/items.csv') AS items
WHERE id > 10 ORDER BY id LIMIT 20;

SELECT active, COUNT(*), AVG(score)
FROM read_ndjson('/srv/seekdb/files/items.jsonl')
GROUP BY active HAVING COUNT(*) > 1;

SELECT c.id, p.score
FROM read_csv('/srv/seekdb/files/items.csv') AS c
LEFT JOIN read_parquet('/srv/seekdb/files/scores.parquet') AS p ON c.id = p.id;

DESCRIBE SELECT * FROM read_csv('/srv/seekdb/files/items.csv');
SELECT file FROM glob('/srv/seekdb/files/*.csv') ORDER BY file;
```

Paths and options must be SQL constants. `read_json` and `read_ndjson` both read
one flat JSON object per line. Nested objects, arrays and duplicate object keys
are rejected. Fields are discovered in first appearance order; missing fields
are NULL. JSON strings retain their string type except for valid ISO dates and
datetimes. CSV/JSONL schema inference scans the entire file before execution;
`sample_size` accepts only `-1`.

CSV options are `header` (default true), `delimiter` (also `delim` or `sep`,
default comma), `quote` and `escape` (default double quote), and `nullstr`
(default empty). An unquoted NULL token becomes SQL NULL; a quoted empty field
remains an empty string. Options use `name = value` syntax. `format` accepts
`csv` for CSV, and `newline_delimited` or `ndjson` for JSONL. Duplicate CSV
headers receive suffixes such as `id__2`. CSV accepts a UTF-8 BOM before the
first field, including a quoted header, and handles LF/CRLF and quoted newlines.

Inference recognizes signed BIGINT, finite DOUBLE, BOOLEAN, VARCHAR, DATE,
DATETIME and all-NULL columns. Integers outside the signed BIGINT range remain
strings. Integer/double mixtures widen to DOUBLE, date/datetime mixtures widen
to DATETIME, and other incompatible mixtures widen to VARCHAR.

Parquet schema comes from its footer. Flat nullable primitive columns, multiple
row groups, uncompressed/Snappy pages and PLAIN, dictionary, delta, boolean RLE
and byte-stream-split encodings are supported. Only referenced columns are
decoded. Unsupported nested types and codecs report an error.

## Export CSV

```sql
COPY (SELECT id, name FROM read_csv('/srv/seekdb/files/items.csv') ORDER BY id)
TO '/srv/seekdb/files/export.csv' (FORMAT csv, HEADER, NULLSTR 'NULL');

COPY my_table TO '/srv/seekdb/files/table.csv'
(HEADER, DELIMITER '|', QUOTE '"', ESCAPE '"', NULLSTR 'NULL');

SELECT * FROM read_csv('/srv/seekdb/files/table.csv', delimiter='|', nullstr='NULL');
```

COPY defaults to no header. All non-NULL output fields are quoted, preserving
empty strings and literal NULL tokens. The NULL token cannot contain the
delimiter, quote or line endings. An existing destination causes an error.
Output is written to a private temporary sibling and published only after the
query completes successfully. Query failures remove the temporary file.

## File safety and limits

Both readers and COPY enforce `secure_file_priv`; an unset or invalid allowed
directory denies access. Readers accept regular files only and never modify
inputs. Path components are opened without following symlinks after canonical
path validation. File identity, size and modification/change timestamps are
checked during discovery, execution and rescan. Changes invalidate the query;
file-dependent plans are not cached. Execution checks the current global
`secure_file_priv` again, including for prepared statements. Glob also checks
the matched file set at scan boundaries to cover coarse directory timestamps.

Scans retain a record or projected Parquet pages rather than all file rows.
Limits include 4,096 columns, 16 MiB per text record/footer, 64 MiB per encoded
Parquet page, 128 MiB of retained decoded Parquet state and 10,000 glob matches.
CSV errors identify the record; JSONL errors identify the line. `glob` discovers
regular files in one directory and does not merge their contents.

Regression coverage is in `unittest/sql/parser/test_file_sql_parser.cpp`,
`unittest/sql/engine/basic/test_file_scan_utils.cpp` and the `file_sql` mysqltest
suite. The suite fixture directory must be inside `secure_file_priv`.

## Linux validation

The recorded Linux ARM64 build, correctness results and full benchmark run are
in [file-sql-linux-validation.md](file-sql-linux-validation.md).

Run the native Linux build and unit targets from the repository root, using the
same dependency preparation as CI:

```sh
python3 build.py init
python3 build.py build -j4 seekdb
python3 build.py test -j4 --test_output=errors \
  //unittest/sql/parser:test_file_sql_parser \
  //unittest/sql/engine/basic:test_file_scan_utils
```

For integration coverage, start the newly built binary as a dedicated local
Linux test instance. Install `pymysql` and `pyarrow` in a test Python environment,
then run:

```sh
python3 unittest/sql/engine/basic/file_sql_regression.py --port 12991 --socket /path/to/instance/run/sql.sock
python3 unittest/storage/fts/tokenize_regression.py --port 12991
```

The file SQL suite creates its own temporary fixtures, temporarily changes
`secure_file_priv`, and restores that setting on completion. It must not run
against a shared instance. Use the Unix socket because changing
`secure_file_priv` requires a socket connection. PyArrow only creates independently encoded test
files; seekdb executes every SQL query. The FTS suite compares complete results
with its bundled pre-optimization reference; it does not infer correctness from
query hit counts alone. Performance scoring still requires the official Linux
benchmark and its unchanged parameters/baseline.

Reader tests cover bounded batches and rescan, cancellation during discovery,
mutation after prefetch, glob changes, malformed/truncated input and COPY
publication/cleanup. SQL tests cover metadata, aliases, NULL/empty values,
full-file inference, native expressions/aggregation, cross-format joins,
Parquet V1/V2 pages, and COPY query/table round trips. Parquet unsigned 32-bit
values widen to BIGINT; unsigned 64-bit values use VARCHAR to preserve the
entire range. Dates and timestamps outside the supported SQL calendar range
are rejected. Each scan prefetches at most 64 rows, with a decoded-byte budget
in addition to the per-record and per-page limits.

The manually dispatched `Competition Linux validation` workflow builds on
Ubuntu 22.04 x86-64, runs both C++ targets and the SQL/FTS regressions, and invokes
the unchanged FTS benchmark with the full competition parameters. Its score is
a reference result on that runner; the official evaluation environment remains
authoritative. The workflow is not triggered merely by editing or pushing code.
