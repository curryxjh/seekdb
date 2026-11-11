"""Independent float32 oracle across dimensions, K, selectivity and mutations."""
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
import uuid

from client import Client, sql_string


def run(binary):
    seed = 14223819  # Held out from the default differential and benchmark seeds.
    rng = random.Random(seed)
    f32 = lambda x: struct.unpack("<f", struct.pack("<f", x))[0]
    checks = 0
    for dim, count in [(1, 257), (17, 4096), (256, 1300)]:
        rows = {i: [i % 100, [f32(rng.uniform(-3, 3)) for _ in range(dim)]]
                for i in range(count)}
        # Nonzero tied vectors test the distance/primary-key tie contract too.
        for key in range(0, count, 37):
            rows[key][1] = [f32(0.75)] * dim
        with tempfile.TemporaryDirectory() as directory:
            client = Client([binary], directory, timeout=120)
            try:
                client.ok(f"CREATE TABLE t(id BIGINT PRIMARY KEY,g BIGINT,v VECTOR({dim}));")
                client.ok("BEGIN;")
                for first in range(0, count, 100):
                    values = [f"({key},{rows[key][0]},{sql_string(json.dumps(rows[key][1]))})"
                              for key in range(first, min(first+100, count))]
                    client.ok("INSERT INTO t VALUES " + ",".join(values) + ";")
                client.ok("COMMIT;")
                q = [f32(0.75)] * dim

                def verify():
                    nonlocal checks
                    for where, predicate in [
                        ("", lambda g: True),
                        (" WHERE g=0", lambda g: g == 0),
                        (" WHERE g<25", lambda g: g < 25),
                        (" WHERE g>=0", lambda g: g >= 0),
                        (" WHERE g<0", lambda g: g < 0),
                    ]:
                        expected = sorted(
                            (math.sqrt(math.fsum((a-b)**2 for a, b in zip(v, q))), key)
                            for key, (g, v) in rows.items() if predicate(g))
                        for k in [0, 1, 10, 63, count+1]:
                            actual = client.ok(
                                f"SELECT id,L2_DISTANCE(v,{sql_string(json.dumps(q))}) AS d "
                                f"FROM t{where} ORDER BY d LIMIT {k};")["rows"]
                            assert [r[0] for r in actual] == [key for d, key in expected[:k]]
                            assert all(math.isclose(r[1], e[0], rel_tol=1e-5, abs_tol=1e-5)
                                       for r, e in zip(actual, expected[:k]))
                            checks += 1

                verify()
                client.ok("CREATE VECTOR INDEX tv ON t(v);")
                client.mode = "ann"
                verify()
                saved = copy.deepcopy(rows)
                client.ok("BEGIN;")
                client.ok(f"UPDATE t SET v={sql_string(json.dumps(q))} WHERE g<25;")
                for g_v in rows.values():
                    if g_v[0] < 25:
                        g_v[1] = list(q)
                client.ok("DELETE FROM t WHERE g>=75;")
                rows = {key: value for key, value in rows.items() if value[0] < 75}
                verify()
                client.ok("ROLLBACK;")
                rows = saved
                verify()
                client.ok("DELETE FROM t WHERE g>=75;")
                rows = {key: value for key, value in rows.items() if value[0] < 75}
                client.close()
                client = Client([binary], directory, timeout=120)
                verify()
            finally:
                client.close()
        print(f"PASS vector sweep seed={seed}, dim={dim}, rows={count}, "
              "filters=none/1%/25%/100%/empty, scan/index/txn/rollback/restart")
    print(f"PASS vector sweep: {checks} exact ordered ID/distance checks, ann exact fallback")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--binary")
    p.add_argument("--image", default="minivectordb:local")
    args = p.parse_args()
    if args.binary:
        run(args.binary)
    else:
        # Reuse the same volume-backed adapter and clean only this run's volumes.
        prefix = "mvdb-sweep-" + uuid.uuid4().hex[:12]
        os.environ.update(MVDB_VOLUME_PREFIX=prefix, MVDB_IMAGE=args.image)
        try:
            run(str(Path(__file__).with_name("docker_db.py").resolve()))
        finally:
            volumes = subprocess.check_output(
                ["docker", "volume", "ls", "-q", "--filter", "label=mvdb-test-run=" + prefix],
                text=True).split()
            if volumes:
                subprocess.run(["docker", "volume", "rm", *volumes], check=True,
                               stdout=subprocess.DEVNULL)
