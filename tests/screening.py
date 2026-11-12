"""Independent exact truth at screening error/overflow/underflow boundaries."""
import argparse
import decimal
import json
import math
from pathlib import Path
import random
import struct
import tempfile

from client import Client, sql_string


def f32(x):
    return struct.unpack("<f", struct.pack("<f", x))[0]


def run(binary):
    checks = 0
    rng = random.Random(927431)
    with tempfile.TemporaryDirectory(prefix="mvdb-screen-") as directory:
        c = Client([binary], directory, timeout=120)
        try:
            for dim in [16, 17, 32, 127, 128, 257, 1024]:
                table = f"t{dim}"
                c.ok(f"CREATE TABLE {table}(id BIGINT PRIMARY KEY,g BIGINT,v VECTOR({dim}));")
                records = {}
                # Nearby shells, intentional ties, mixed exponents, float-square
                # overflow, and subnormal cancellation. Reverse insertion order
                # prevents accidental insertion-order tie breaking.
                for key in range(159, -1, -1):
                    scale = [1.0, 1e-22, 1e-38, 1e20, 1e38][key % 5]
                    vector = [f32(scale * (1 + rng.choice([-1, 0, 1]) * 2**-23)) for _ in range(dim)]
                    if key % 7 == 0:
                        vector = [f32(scale)] * dim
                    if key % 11 == 0:
                        vector = [f32(rng.choice([-1, 1]) * scale)] * dim
                    if key % 7 != 0:
                        # Keep non-identical shells closer than the screening
                        # bound but outside ambiguous 1-ULP double norm ties.
                        vector[0] = f32(scale * (1 + key * 2**-15))
                    records[key] = vector
                c.ok("BEGIN;")
                for start in range(0, 160, 40):
                    keys = list(records)[start:start+40]
                    values = [f"({key},{key%5},{sql_string(json.dumps(records[key]))})" for key in keys]
                    c.ok(f"INSERT INTO {table} VALUES " + ",".join(values) + ";")
                c.ok("COMMIT;")

                def verify():
                    nonlocal checks
                    decimal.getcontext().prec = 100
                    stored = {key: [decimal.Decimal(x) for x in vector] for key, vector in records.items()}
                    for scale in [0.0, 1.0, 1e-22, 1e-38, 1e20, -1e38]:
                        query = [f32(scale)] * dim
                        dq = decimal.Decimal(query[0])
                        # Older Python math.dist can differ by 1 ULP here;
                        # decimal arithmetic avoids calling those false ties.
                        distances = {key: float(sum((x-dq)**2 for x in vector).sqrt())
                                     for key, vector in stored.items()}
                        for group in [None, 0, 1, 2, 3, 4, 99]:
                            where = "" if group is None else f" WHERE g={group}"
                            candidates = [(distances[key], key) for key in records
                                          if group is None or key % 5 == group]
                            for desc in [False, True]:
                                wanted = sorted(candidates, key=lambda x: (-x[0] if desc else x[0], x[1]))[:10]
                                rows = c.ok(f"SELECT id,L2_DISTANCE(v,{sql_string(json.dumps(query))}) AS d "
                                            f"FROM {table}{where} ORDER BY d " +
                                            ("DESC " if desc else "") + "LIMIT 10;")["rows"]
                                assert [r[0] for r in rows] == [key for _, key in wanted], (dim, scale, group, desc, rows, wanted)
                                for (_, actual), (expected, _) in zip(rows, wanted):
                                    assert math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-300)
                                checks += 1
                verify()
                c.ok(f"CREATE VECTOR INDEX i{dim} ON {table}(v);")
                verify()
            # User's actual prediction: filter before search. Global nearest
            # ten all fail the predicate; the correct answer is the distant ten.
            c.ok("CREATE TABLE filtered(id BIGINT PRIMARY KEY,g BIGINT,v VECTOR(16));")
            c.ok("BEGIN;")
            for start in range(0, 10000, 100):
                values = [f"({key},{1 if key>=9900 else 0},{sql_string(json.dumps([key]*16))})"
                          for key in range(start, start+100)]
                c.ok("INSERT INTO filtered VALUES " + ",".join(values) + ";")
            c.ok("COMMIT;")
            answer = c.ok("SELECT id FROM filtered WHERE g=1 ORDER BY L2_DISTANCE(v,'[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]') LIMIT 10;")["rows"]
            assert answer == [[key] for key in range(9900, 9910)], answer
            print("FILTER counterexample: global Top10 ids=0..9 (all g=0); WHERE g=1 -> ids=" + json.dumps(answer), flush=True)
            transferred = c.ok("SELECT id FROM filtered WHERE g=1 ORDER BY L2_DISTANCE(v,'[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]') LIMIT 200;")["rows"]
            assert transferred == [[key] for key in range(9900, 10000)], transferred
            print("FILTER transfer LIMIT 200: returned_count=" + str(len(transferred)) +
                  ", first=" + str(transferred[0][0]) + ", last=" + str(transferred[-1][0]), flush=True)
            # Force a global quantization step of 1. Both near vectors have
            # integer code 1, but id=2 is genuinely closer than id=1. Refining
            # only the integer Top1 cannot repair the missing candidate.
            c.ok("CREATE TABLE quantized_tie(id BIGINT PRIMARY KEY,v VECTOR(32));")
            vectors = {1: [1.49]+[0]*31, 2: [1.01]+[0]*31}
            vectors.update({i: [127]+[0]*31 for i in range(3, 303)})
            c.ok("BEGIN;")
            for start in range(1, 303, 100):
                values = [f"({i},{sql_string(json.dumps(vectors[i]))})" for i in range(start, min(start+100,303))]
                c.ok("INSERT INTO quantized_tie VALUES " + ",".join(values) + ";")
            c.ok("COMMIT;")
            query = sql_string(json.dumps([0]*32))
            exact = c.ok(f"SELECT id,L2_DISTANCE(v,{query}) AS d FROM quantized_tie ORDER BY d LIMIT 1;")["rows"]
            assert exact == [[2, f32(1.01)]], exact
            print("QUANTIZATION counterexample: approximate Top1 would choose id=1 (code tie); exact returned id=2, distance="+
                  str(exact[0][1]), flush=True)
        finally:
            c.close()
    print(f"PASS screening: {checks} exact ID/order/distance checks; seed=927431, ties, near-ties, overflow, subnormals, sparse filters, DESC, scan/index")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--binary", required=True)
    run(str(Path(p.parse_args().binary).resolve()))
