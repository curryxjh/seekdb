import argparse
import copy
import json
import math
import os
from pathlib import Path
import random
import struct
import subprocess
import tempfile

from client import Client, sql_string


def f32(value):
    return struct.unpack("<f", struct.pack("<f", value))[0]


def check_rows(client, sql, expected):
    actual = client.ok(sql)["rows"]
    assert actual == expected, (sql, actual, expected)


def bad(client, sql, code=None):
    result = client.sql(sql)
    assert not result["ok"] and result.get("error"), (sql, result)
    if code:
        assert result["error"] == code, (sql, result, code)


def boundaries(client):
    bad(client, "CREATE TABLE x (id BIGINT);")
    bad(client, "CREATE TABLE x (id VARCHAR(4) PRIMARY KEY);")
    bad(client, "CREATE TABLE x (id BIGINT PRIMARY KEY, id BIGINT);")
    bad(client, "CREATE TABLE x (id BIGINT PRIMARY KEY, a VECTOR(1), b VECTOR(1));")
    client.ok("CREATE TABLE t (id BIGINT PRIMARY KEY, n BIGINT, s VARCHAR(4), v VECTOR(2));")
    client.ok("INSERT INTO t VALUES (-9223372036854775808,9223372036854775807,'中文😀a','[16777217,0]');")
    check_rows(client, "SELECT id,n,s FROM t;", [[-(2**63), 2**63-1, "中文😀a"]])
    for integer in ["9223372036854775808", "-9223372036854775809", "1.0", "1e0"]:
        bad(client, f"INSERT INTO t VALUES ({integer},0,'x','[0,0]');")
    for vector in ["[0]", "[0,0,0]", "[NaN,0]", "[Infinity,0]", "[1e39,0]",
                   "[1e999,0]", '[\"x\",0]', "[0,]", "[0 0]", "[0x1,0]"]:
        bad(client, f"INSERT INTO t VALUES (1,0,'x',{sql_string(vector)});", "invalid_vector")
    bad(client, "INSERT INTO t VALUES (1,0,'中文😀ab','[0,0]');", "value_too_long")
    client.ok("INSERT INTO t VALUES (1,0,'é','[16777216,0]'),(2,0,'😀','[-0,1e-999]');")
    check_rows(client, "SELECT id FROM t ORDER BY s;", [[1], [-(2**63)], [2]])
    check_rows(client, "SELECT id,L2_DISTANCE(v,'[16777216,0]') AS d FROM t ORDER BY d LIMIT 2;",
               [[-(2**63), 0], [1, 0]])
    bad(client, "UPDATE t SET id=4 WHERE id=1;")
    bad(client, "SELECT absent FROM t WHERE id=9999;")
    bad(client, "SELECT id FROM t WHERE absent=0;")
    bad(client, "SELECT id FROM t LIMIT -1;")
    bad(client, "INSERT INTO t VALUES (3,0,'x','[0,0]') garbage;")
    check_rows(client, "SELECT id FROM t WHERE n=5;", [])
    check_rows(client, "SELECT id FROM t WHERE s='longer than the column';", [])
    assert client.ok("UPDATE t SET n=1 WHERE s='longer than the column';")["affected_rows"] == 0
    assert client.ok("DELETE FROM t WHERE s='longer than the column';")["affected_rows"] == 0
    check_rows(client, "SELECT id FROM t LIMIT 0;", [])
    client.ok("CREATE TABLE quoted (id BIGINT PRIMARY KEY, s VARCHAR(64));")
    client.ok("/* ; */ INSERT INTO quoted VALUES\n(1,'a;,b''c\\nd'), -- ignored ;\n(2,'x\\\\y');")
    check_rows(client, "SELECT s FROM quoted ORDER BY id;", [["a;,b'c\nd"], ["x\\y"]])
    client.ok("INSERT INTO quoted VALUES(3,'first\nsecond');")
    check_rows(client, "SELECT s FROM quoted WHERE id=3;", [["first\nsecond"]])
    # The stream permits separate SQL statements; trailing garbage *within* one
    # frame is rejected. The parser API is never asked to silently ignore tails.
    client.process.stdin.write(b"INSERT INTO quoted VALUES(4,'\xed\xa0\x80');\n")
    assert client.receive()["error"] == "invalid_unicode"
    check_rows(client, "SELECT id FROM quoted ORDER BY id;", [[1], [2], [3]])
    client.ok("BEGIN;")
    client.ok("UPDATE quoted SET s='kept' WHERE id=1;")
    bad(client, "INSERT INTO quoted VALUES (4,'new'),(1,'duplicate');", "duplicate_key")
    client.ok("INSERT INTO quoted VALUES (5,'last');")
    client.ok("COMMIT;")
    check_rows(client, "SELECT id,s FROM quoted ORDER BY id;",
               [[1, "kept"], [2, "x\\y"], [3, "first\nsecond"], [5, "last"]])
    client.ok("BEGIN;")
    client.ok("DELETE FROM quoted WHERE id>=1;")
    bad(client, "INSERT INTO quoted VALUES (8,'ok'),(9,'0123456789" + "a"*60 + "');")
    client.ok("ROLLBACK;")
    check_rows(client, "SELECT id FROM quoted ORDER BY id;", [[1], [2], [3], [5]])
    client.ok("CREATE VECTOR INDEX t_vec ON t(v);")
    client.ok("BEGIN;")
    client.ok("DELETE FROM t WHERE id=1;")
    client.ok("INSERT INTO t VALUES(1,1,'重插','[0,0]');")
    client.ok("UPDATE t SET v='[0,0]',s='修改' WHERE id=2;")
    check_rows(client, "SELECT id FROM t ORDER BY L2_DISTANCE(v,'[0,0]') LIMIT 2;", [[1], [2]])
    client.ok("\\mode ann")
    check_rows(client, "SELECT id FROM t WHERE n=1 ORDER BY L2_DISTANCE(v,'[0,0]') LIMIT 9;", [[1]])
    client.ok("ROLLBACK;")
    check_rows(client, "SELECT s FROM t WHERE id=2;", [["😀"]])
    print("PASS boundaries, framing, Unicode, float32, statement and index transaction atomicity")


def differential(client, seed, steps=150):
    rng = random.Random(seed)
    rows, saved = {}, None
    indexed = False
    client.ok("CREATE TABLE r(id BIGINT PRIMARY KEY,g BIGINT,s VARCHAR(16),v VECTOR(4));")
    for step in range(steps):
        if step == 25:
            client.ok("CREATE VECTOR INDEX rv ON r(v);")
            indexed = True
        operation = rng.choice(["insert", "insert", "update", "delete", "begin",
                                "commit", "rollback", "restart", "failed_batch"])
        if operation in ("insert", "failed_batch"):
            key = rng.randrange(-15, 30)
            group = rng.randrange(4)
            text = rng.choice(["中文", "😀", "a;b", "é", "quote's", ""])
            vector = [rng.uniform(-4, 4) for _ in range(4)]
            item = [group, text, [f32(x) for x in vector]]
            literal = f"({key},{group},{sql_string(text)},{sql_string(json.dumps(vector))})"
            sql = f"INSERT INTO r VALUES {literal}"
            if operation == "failed_batch":
                sql += "," + literal
                bad(client, sql + ";", "duplicate_key")
            elif key in rows:
                bad(client, sql + ";", "duplicate_key")
            else:
                client.ok(sql + ";")
                rows[key] = item
        elif operation == "update":
            group = rng.randrange(4)
            vector = [rng.uniform(-8, 8) for _ in range(4)]
            client.ok(f"UPDATE r SET v={sql_string(json.dumps(vector))},s='updated' WHERE g={group};")
            for item in rows.values():
                if item[0] == group:
                    item[1:] = ["updated", [f32(x) for x in vector]]
        elif operation == "delete":
            boundary = rng.randrange(-15, 30)
            client.ok(f"DELETE FROM r WHERE id<{boundary};")
            rows = {key: value for key, value in rows.items() if key >= boundary}
        elif operation == "begin":
            if saved is None:
                client.ok("BEGIN;")
                saved = copy.deepcopy(rows)
            else:
                bad(client, "BEGIN;", "transaction_active")
        elif operation == "commit":
            client.ok("COMMIT;")
            saved = None
        elif operation == "rollback":
            client.ok("ROLLBACK;")
            if saved is not None:
                rows, saved = saved, None
        elif operation == "restart":
            client.close()
            client = Client(client.command, client.directory)
            if saved is not None:
                rows, saved = saved, None
            # Index DDL itself is transactional, and may have rolled back.
        expected = [[key, *rows[key]] for key in sorted(rows)]
        check_rows(client, "SELECT * FROM r ORDER BY id;", expected)
        group = rng.choice([None, 0, 1, 2, 3, 7])
        q = [f32(rng.uniform(-6, 6)) for _ in range(4)]
        k = rng.choice([0, 1, 3, 10, 100])
        truth = []
        for key, (g, text, vector) in rows.items():
            if group is None or g == group:
                d = math.sqrt(math.fsum((float(a)-float(b))**2 for a, b in zip(vector, q)))
                truth.append((d, key))
        truth.sort()
        where = "" if group is None else f" WHERE g={group}"
        actual = client.ok(f"SELECT id,L2_DISTANCE(v,{sql_string(json.dumps(q))}) AS d "
                           f"FROM r{where} ORDER BY d LIMIT {k};")["rows"]
        assert [r[0] for r in actual] == [key for d, key in truth[:k]], (seed, step, actual, truth)
        for (_, actual_d), (expected_d, _) in zip(actual, truth[:k]):
            assert math.isclose(actual_d, expected_d, rel_tol=1e-5, abs_tol=1e-5)
    client.close()
    print(f"PASS differential seed={seed}, steps={steps}, index_attempted={indexed}")


def run(binary, temp_root=None, seeds=None):
    with tempfile.TemporaryDirectory(dir=temp_root) as directory:
        client = Client([binary], directory)
        try:
            boundaries(client)
            # Directory lock must fail before ready and release on process exit.
            locked = subprocess.run([binary, "--data", directory], input=b"", capture_output=True)
            assert locked.returncode != 0 and not locked.stdout and b"in use" in locked.stderr
            client.close()
            client = Client([binary], directory)
            check_rows(client, "SELECT id FROM quoted ORDER BY id;", [[1], [2], [3], [5]])
        finally:
            client.close()
    for seed in seeds or [7, 42, 20261109, 8675309, 39034843]:
        with tempfile.TemporaryDirectory(dir=temp_root) as directory:
            differential(Client([binary], directory), seed)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--binary", required=True)
    p.add_argument("--temp-root")
    p.add_argument("--seeds", nargs="+", type=int)
    args = p.parse_args()
    run(args.binary, args.temp_root, args.seeds)
