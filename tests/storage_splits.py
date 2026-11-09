"""Exercise multi-level splits, cache eviction and transaction snapshots via SQL."""
import argparse
import random
import tempfile

from client import Client


def run(binary, temp_root=None):
    count = 66000  # Exceeds 255 * 255 leaf/internal fanout.
    seed = 30274159
    keys = [2 * i - count for i in range(count)]
    keys += [-(2**63), 2**63 - 1]
    expected = [[key, -key if key != -(2**63) else 0] for key in sorted(keys)]
    with tempfile.TemporaryDirectory(dir=temp_root) as directory:
        client = Client([binary], directory, timeout=120)
        try:
            for order in ["ascending", "descending", "shuffled"]:
                sequence = sorted(keys, reverse=order == "descending")
                if order == "shuffled":
                    random.Random(seed).shuffle(sequence)
                client.ok(f"CREATE TABLE {order}(id BIGINT PRIMARY KEY,n BIGINT);")
                client.ok("BEGIN;")
                for first in range(0, len(sequence), 500):
                    values = ",".join(
                        f"({key},{-key if key != -(2**63) else 0})"
                        for key in sequence[first:first + 500])
                    client.ok(f"INSERT INTO {order} VALUES {values};")
                client.ok("COMMIT;")
                assert client.ok(f"SELECT * FROM {order};")["rows"] == expected
                for key in keys[::251] + keys[-2:]:
                    value = -key if key != -(2**63) else 0
                    assert client.ok(f"SELECT n FROM {order} WHERE id={key};")["rows"] == [[value]]
                client.ok("BEGIN;")
                client.ok(f"UPDATE {order} SET n=7 WHERE id=0;")
                client.ok(f"DELETE FROM {order} WHERE id<=-65000;")
                values = ",".join(f"({key},1)" for key in range(-15, 16, 2))
                client.ok(f"INSERT INTO {order} VALUES {values};")
                client.ok("ROLLBACK;")
                assert client.ok(f"SELECT * FROM {order};")["rows"] == expected
                client.ok("BEGIN;")
                client.ok(f"UPDATE {order} SET n=7 WHERE id=0;")
                result = client.sql(f"INSERT INTO {order} VALUES (3,1),(0,1);")
                assert not result["ok"] and result["error"] == "duplicate_key"
                assert client.ok(f"SELECT id FROM {order} WHERE id=3;")["rows"] == []
                client.ok("COMMIT;")
                client.ok("BEGIN;")
                values = ",".join(f"({key},1)" for key in range(66000, 68000, 2))
                client.ok(f"INSERT INTO {order} VALUES {values};")
                client.kill()
                client = Client([binary], directory, timeout=120)
                assert client.ok(f"SELECT id FROM {order} WHERE id=66000;")["rows"] == []
                final = [[key, 7 if key == 0 else value] for key, value in expected]
                assert client.ok(f"SELECT * FROM {order};")["rows"] == final
                print(f"PASS tree splits order={order}, rows={len(keys)}, seed={seed}, "
                      "point lookup, rollback, failed statement, SIGKILL and restart")
            client.close()
            client = Client([binary], directory, timeout=120)
            for order in ["ascending", "descending", "shuffled"]:
                assert client.ok(f"SELECT * FROM {order};")["rows"] == final
            print("PASS all split trees after a second restart")
        finally:
            client.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--binary", required=True)
    p.add_argument("--temp-root")
    args = p.parse_args()
    run(args.binary, args.temp_root)
