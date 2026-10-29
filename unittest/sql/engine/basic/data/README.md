Parquet fixtures are base64 encoded to keep test data independent of the SQL
server's filesystem deployment scripts.

- `simple.parquet.b64`: the existing seven-integer regression fixture.
- `delta.parquet.b64` and `dictionary.parquet.b64`: 300 rows, nullable primitive
  columns, three row groups, Snappy V2 pages, dictionary/delta/byte-stream-split
  encodings.
- `unsigned-null-v*.parquet.b64`: generated with PyArrow 26.0.0, three one-row
  groups, Snappy compression, data page versions 1.0 and 2.0. The columns are
  UINT32 `[0, 4294967295, null]`, UINT64 `[0, 18446744073709551615, null]`, and an
  all-NULL column. Tests assert the values and inferred types independently.

The integration test creates additional Parquet files using PyArrow at runtime;
it uses seekdb alone for query execution.
