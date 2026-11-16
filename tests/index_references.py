"""Exact indexed scans above the heap cutoff, including mixed old/new postings."""
import argparse
import copy
import json
import math
from pathlib import Path
import struct
import tempfile

from client import Client, sql_string


def run(binary, legacy_binary=None):
    binary = str(Path(binary).resolve())
    creator = str(Path(legacy_binary).resolve()) if legacy_binary else binary
    f32 = lambda x: struct.unpack("<f", struct.pack("<f", x))[0]
    checks = 0
    for dimension in (1, 17, 128):
        rows = {i: [i % 4, [f32(((i * 17 + j) % 19 - 9) / 8)
                           for j in range(dimension)]] for i in range(-65, 192)}
        for i in range(-65, 192, 13):
            rows[i][1] = [f32(0.75)] * dimension
        with tempfile.TemporaryDirectory() as directory:
            client = Client([creator], directory, timeout=120)
            client.ok(f"CREATE TABLE t(id BIGINT PRIMARY KEY,g BIGINT,s VARCHAR(32),"
                      f"v VECTOR({dimension}));")
            for start in range(-65, 192, 64):
                values = [f"({i},{rows[i][0]},'界-{i}',"
                          f"{sql_string(json.dumps(rows[i][1]))})"
                          for i in range(start, min(start + 64, 192))]
                client.ok("INSERT INTO t VALUES " + ",".join(values) + ";")
            client.ok("CREATE VECTOR INDEX tv ON t(v);")
            client.close()
            client = Client([binary], directory, timeout=120)

            def verify():
                nonlocal checks
                for mode in ("exact", "ann"):
                    client.mode = mode
                    for query in ([0.0] * dimension, [0.75] * dimension):
                        distances = {i: math.sqrt(math.fsum((a - b) ** 2
                                     for a, b in zip(row[1], query)))
                                     for i, row in rows.items()}
                        for descending in (False, True):
                            expected = sorted(rows, key=lambda i:
                                              ((-1 if descending else 1) * distances[i], i))
                            for k in (10, 20000):
                                # LIMIT 20000 + no predicate forces the external
                                # sorter/index path even with this small dataset.
                                actual = client.ok(
                                    f"SELECT id,g,s,L2_DISTANCE(v,"
                                    f"{sql_string(json.dumps(query))}) AS d FROM t "
                                    f"ORDER BY d {'DESC' if descending else 'ASC'} "
                                    f"LIMIT {k};")["rows"]
                                assert [r[0] for r in actual] == expected[:k], (mode, k)
                                for i, group, text, distance in actual:
                                    assert group == rows[i][0] and text == f"界-{i}"
                                    assert math.isclose(distance, distances[i],
                                                        rel_tol=1e-12, abs_tol=1e-12)
                                checks += 1
                stored = client.ok("SELECT id,v FROM t ORDER BY id;")["rows"]
                assert stored == [[i, rows[i][1]] for i in sorted(rows)]
                checks += 1

            try:
                verify()
                saved = copy.deepcopy(rows)
                client.ok("BEGIN;")
                vector = [f32(-0.5)] * dimension
                client.ok(f"UPDATE t SET v={sql_string(json.dumps(vector))} WHERE g=1;")
                for i in rows:
                    if rows[i][0] == 1:
                        rows[i][1] = vector[:]
                client.ok("DELETE FROM t WHERE g=2;")
                rows = {i: r for i, r in rows.items() if r[0] != 2}
                # First row is new; duplicate second row must undo both roots.
                response = client.sql(f"INSERT INTO t VALUES"
                                      f"(1000,0,'new',{sql_string(json.dumps(vector))}),"
                                      f"(0,0,'duplicate',{sql_string(json.dumps(vector))});")
                assert not response["ok"], response
                verify()
                client.ok("ROLLBACK;")
                rows = saved
                verify()
                client.ok(f"UPDATE t SET v={sql_string(json.dumps(vector))} WHERE g=1;")
                for i in rows:
                    if rows[i][0] == 1:
                        rows[i][1] = vector[:]
                client.ok("DELETE FROM t WHERE g=2;")
                rows = {i: r for i, r in rows.items() if r[0] != 2}
                # Legacy builder leaves inline postings; updates now create
                # reference postings in the same index and committed catalog.
                client.close()
                client = Client([binary], directory, timeout=120)
                verify()
                client.ok("BEGIN;")
                client.ok("DELETE FROM t;")
                client.kill()
                client = Client([binary], directory, timeout=120)
                verify()
                client.ok("BEGIN;")
                client.ok("DELETE FROM t;")
                client.close()
                client = Client([binary], directory, timeout=120)
                verify()
            finally:
                client.close()
        print(f"PASS reference postings: D={dimension}, legacy={bool(legacy_binary)}, "
              "heap/external-sort, ASC/DESC, ties, mutation, failed insert, "
              "rollback, shutdown and SIGKILL/restart")
    print(f"PASS reference postings: {checks} exact checks")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True)
    parser.add_argument("--legacy-binary")
    args = parser.parse_args()
    run(args.binary, args.legacy_binary)
