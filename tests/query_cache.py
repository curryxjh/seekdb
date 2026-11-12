"""Cache prefix/tail, table switches, EOF reuse and result-spill boundaries."""
import argparse
import json
from pathlib import Path
import tempfile

from client import Client, sql_string


def run(binary):
    checks = 0
    with tempfile.TemporaryDirectory(prefix="mvdb-cache-") as directory:
        c = Client([binary], directory, timeout=120)
        try:
            for name, dim, count in [("wide", 1024, 2200), ("tiny", 1, 53), ("quantized", 128, 600)]:
                c.ok(f"CREATE TABLE {name}(id BIGINT PRIMARY KEY,g BIGINT,v VECTOR({dim}));")
                c.ok("BEGIN;")
                for start in range(0, count, 100):
                    rows = [f"({i},{i%4},{sql_string(json.dumps([i] * dim))})"
                            for i in range(start, min(start + 100, count))]
                    c.ok(f"INSERT INTO {name} VALUES " + ",".join(rows) + ";")
                c.ok("COMMIT;")

            def check(name, dim, target, ids, where="", desc=False):
                nonlocal checks
                # Identical dimensions make independent integer abs() the oracle.
                wanted = sorted(ids, key=lambda i: ((-1 if desc else 1) * abs(i-target), i))[:10]
                q = sql_string(json.dumps([target] * dim))
                rows = c.ok(f"SELECT id FROM {name}{where} ORDER BY L2_DISTANCE(v,{q})"
                            + (" DESC" if desc else "") + " LIMIT 10;")["rows"]
                assert rows == [[i] for i in wanted], (name, target, rows, wanted)
                checks += 1

            for _ in range(2):
                # The nearest records are beyond the 8 MiB prefix.
                check("wide", 1024, 2177, range(2200))
                check("wide", 1024, 2177, range(2, 2200, 4), " WHERE g=2")
                check("wide", 1024, 2177, range(2200), desc=True)
                check("tiny", 1, 31, range(53))
            # More than four predicates evict masks; changed literals and
            # comparison operators must never reuse another predicate's mask.
            for limit in [1, 2, 3, 4, 0, 2, 1]:
                check("tiny", 1, 31, [i for i in range(53) if i % 4 < limit],
                      f" WHERE g<{limit}")
                check("tiny", 1, 31, [i for i in range(53) if i % 4 >= limit],
                      f" WHERE g>={limit}")
            # Same offsets can be allocated again after rollback or a late error.
            for action in ["rollback", "failed_insert"]:
                c.ok("BEGIN;")
                c.ok("DELETE FROM tiny WHERE id=31;")
                check("tiny", 1, 31, [i for i in range(53) if i != 31])
                if action == "failed_insert":
                    result = c.sql("INSERT INTO tiny VALUES(100,0,'[31]'),(1,0,'[99]');")
                    assert not result["ok"]
                    check("tiny", 1, 31, [i for i in range(53) if i != 31])
                c.ok("ROLLBACK;")
                check("tiny", 1, 31, range(53))
            # Exercise quantized screening + interval cutoffs on a live root,
            # a transaction root, root-offset reuse after failure, and rollback.
            check("quantized", 128, 310, range(600))
            c.ok("BEGIN;")
            c.ok("DELETE FROM quantized WHERE id=310;")
            check("quantized", 128, 310, [i for i in range(600) if i != 310])
            result = c.sql("INSERT INTO quantized VALUES(999,0,'[" + ",".join(["310"]*128) +
                           "]'),(1,0,'[" + ",".join(["999"]*128) + "]');")
            assert not result["ok"]
            check("quantized", 128, 310, [i for i in range(600) if i != 310])
            c.ok("ROLLBACK;")
            check("quantized", 128, 310, range(600))
            c.ok("BEGIN;")
            c.ok("UPDATE quantized SET v='[" + ",".join(["10000"]*128) + "]' WHERE id=310;")
            check("quantized", 128, 310, [i for i in range(600) if i != 310])
            c.ok("COMMIT;")
            check("quantized", 128, 310, [i for i in range(600) if i != 310])
            c.ok("CREATE TABLE empty(id BIGINT PRIMARY KEY,v VECTOR(1));")
            assert c.ok("SELECT id FROM empty ORDER BY L2_DISTANCE(v,'[0]') LIMIT 10;")["rows"] == []
            # Small response, >4KiB response, small response on one connection.
            c.ok("CREATE TABLE text(id BIGINT PRIMARY KEY,s VARCHAR(8000));")
            for n in [4000, 4096, 7000]:
                text = "界" * n
                c.ok("BEGIN;")
                c.ok(f"INSERT INTO text VALUES(1,{sql_string(text)});")
                assert c.ok("SELECT s FROM text;")["rows"] == [[text]]
                c.ok("ROLLBACK;")
                assert c.ok("SELECT s FROM text;")["rows"] == []
                checks += 2
            c.close()
            c = Client([binary], directory, timeout=120)
            check("wide", 1024, 2177, range(2200))
            check("tiny", 1, 31, range(53))
        finally:
            c.close()
    print(f"PASS query cache: {checks} checks; prefix/tail, dimensions, rollback, failed statement, restart, spill")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--binary", required=True)
    run(str(Path(p.parse_args().binary).resolve()))
