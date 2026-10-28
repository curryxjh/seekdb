"""Regenerate Parquet fixtures; PyArrow is needed only for this developer step.

The database and its tests decode the checked-in files without PyArrow.
"""
import base64
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


def main():
    count = 300
    table = pa.table({
        "id": pa.array([i - 150 if i % 7 else None for i in range(count)], type=pa.int64()),
        "text": pa.array([f"中文_{i}_suffix" if i % 5 else None for i in range(count)]),
        "flag": pa.array([bool(i % 2) if i % 3 else None for i in range(count)]),
        "score": pa.array([i * 0.25 if i % 9 else None for i in range(count)]),
        "empty": pa.nulls(count, pa.int64()),
        "ts": pa.array([i * 123456789 if i % 6 else None for i in range(count)], type=pa.timestamp("ns")),
    })
    root = Path(__file__).parent
    for dictionary in (False, True):
        options = {} if dictionary else {"column_encoding": {
            "id": "DELTA_BINARY_PACKED", "text": "DELTA_BYTE_ARRAY", "score": "BYTE_STREAM_SPLIT"}}
        stream = pa.BufferOutputStream()
        pq.write_table(table, stream, compression="snappy", row_group_size=110,
                       data_page_version="2.0", use_dictionary=dictionary, **options)
        name = "dictionary" if dictionary else "delta"
        encoded = base64.b64encode(stream.getvalue().to_pybytes()).decode("ascii")
        (root / f"{name}.parquet.b64").write_text(encoded + "\n", encoding="ascii")


if __name__ == "__main__":
    main()
