"""Deterministic SIGKILL tests against a build compiled with MINIVECTORDB_FAULTS."""
import argparse
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from client import Client


def state(client):
    return (client.ok("SELECT * FROM a ORDER BY id;")["rows"],
            client.ok("SELECT * FROM b ORDER BY id;")["rows"])


def run(binary, temp_root=None):
    binary = str(Path(binary).resolve())
    points = ["before_data_flush", "after_data_flush", "after_data_sync",
              "after_catalog_write", "after_catalog_sync", "after_catalog_rename",
              "after_directory_sync", "before_success_response", "after_success_response"]
    recovery = ["recovery_start", "recovery_before_truncate", "recovery_after_truncate", "recovery_ready"]
    old = ([[1, 10]], [[1, 10]])
    new = ([[1, 10], [2, 20]], [[1, 20]])
    with tempfile.TemporaryDirectory(dir=temp_root) as root:
        baseline = Path(root) / "baseline"
        client = Client([binary], baseline)
        for table in ["a", "b"]:
            client.ok(f"CREATE TABLE {table}(id BIGINT PRIMARY KEY,n BIGINT);")
            client.ok(f"INSERT INTO {table} VALUES(1,10);")
        client.close()
        for point in points:
            directory = Path(root) / point
            shutil.copytree(baseline, directory)
            env = {"MVDB_FAULT": point}
            if "success_response" in point:
                env["MVDB_FAULT_HIT"] = "4"
            client = Client([binary], directory, env)
            acknowledged = []
            for sql in ["BEGIN;", "INSERT INTO a VALUES(2,20);", "UPDATE b SET n=20 WHERE id=1;"]:
                acknowledged.append((sql, client.ok(sql)))
            client.send("COMMIT;")
            try:
                answer = client.receive()
                assert answer["ok"]
                acknowledged.append(("COMMIT;", answer))
                commit_acknowledged = True
            except EOFError:
                commit_acknowledged = False
            client.process.wait(timeout=30)
            assert client.process.returncode == -9, (point, client.process.returncode)
            client.kill()
            # Kill recovery four times on the same directory before checking.
            for stage in recovery:
                process = subprocess.run([binary, "--data", str(directory)], input=b"",
                                         capture_output=True, env={**os.environ, "MVDB_FAULT": stage},
                                         timeout=30)
                assert process.returncode == -9 and not process.stdout, (point, stage, process)
            for attempt in range(2):
                client = Client([binary], directory)
                actual = state(client)
                assert actual in [old, new], (point, actual)
                if commit_acknowledged:
                    assert actual == new, (point, acknowledged, actual)
                client.close()
            print(f"PASS crash={point}, commit_ack={commit_acknowledged}, "
                  f"recovered={'new' if actual == new else 'old'}, recovery_kills=4, idempotent_starts=2")
        directory = Path(root) / "uncommitted"
        shutil.copytree(baseline, directory)
        client = Client([binary], directory)
        client.ok("BEGIN;")
        client.ok("INSERT INTO a VALUES(2,20);")
        client.ok("UPDATE b SET n=20 WHERE id=1;")
        assert state(client) == new
        client.kill()
        client = Client([binary], directory)
        assert state(client) == old
        client.close()
        print("PASS SIGKILL without COMMIT: entire multi-table transaction absent")
        # Auto-commit uses exactly the same durable publication boundary.
        for point in points[:7]:
            directory = Path(root) / ("auto-" + point)
            shutil.copytree(baseline, directory)
            client = Client([binary], directory, {"MVDB_FAULT": point})
            client.send("INSERT INTO a VALUES(2,20),(3,30);")
            try:
                client.receive()
                raise AssertionError("fault did not trigger")
            except EOFError:
                pass
            client.process.wait(timeout=30)
            assert client.process.returncode == -9
            client.kill()
            client = Client([binary], directory)
            actual = state(client)
            assert actual in [old, ([[1, 10], [2, 20], [3, 30]], [[1, 10]])], (point, actual)
            client.close()
            print(f"PASS autocommit crash={point}: atomic batch")
    print("PASS crash suite: 17 kill scenarios, 36 repeated recovery kills, 18 idempotent restarts")
    with tempfile.TemporaryDirectory(dir=temp_root) as root:
        baseline = Path(root) / "vector-baseline"
        client = Client([binary], baseline)
        client.ok("CREATE TABLE v(id BIGINT PRIMARY KEY,g BIGINT,v VECTOR(2));")
        client.ok("INSERT INTO v VALUES(1,1,'[0,0]'),(2,2,'[3,4]');")
        client.close()
        cases = [
            ("index-append", False, "CREATE VECTOR INDEX vi ON v(v);", "after_blob_write", 1),
            ("index-publish", False, "CREATE VECTOR INDEX vi ON v(v);", "after_catalog_rename", 1),
            ("row-before-write", True, "INSERT INTO v VALUES(3,3,'[1,1]');", "before_blob_write", 1),
            ("row-append", True, "INSERT INTO v VALUES(3,3,'[1,1]');", "after_blob_write", 1),
            ("posting-append", True, "UPDATE v SET v='[1,1]' WHERE id=2;", "after_blob_write", 2),
            ("indexed-publish", True, "UPDATE v SET v='[1,1]' WHERE id=2;", "after_catalog_rename", 1),
        ]
        for name, indexed, sql, point, hit in cases:
            directory = Path(root) / name
            shutil.copytree(baseline, directory)
            if indexed:
                client = Client([binary], directory)
                client.ok("CREATE VECTOR INDEX vi ON v(v);")
                client.close()
            client = Client([binary], directory,
                            {"MVDB_FAULT": point, "MVDB_FAULT_HIT": str(hit)})
            client.send(sql)
            try:
                client.receive()
                raise AssertionError("fault did not trigger")
            except EOFError:
                pass
            client.process.wait(timeout=30)
            assert client.process.returncode == -9
            client.kill()
            client = Client([binary], directory)
            changed = name == "indexed-publish"
            expected = [[1, 1, [0, 0]], [2, 2, [1, 1] if changed else [3, 4]]]
            assert client.ok("SELECT * FROM v ORDER BY id;")["rows"] == expected
            distance = math.sqrt(2) if changed else 5
            actual = client.ok("SELECT id,L2_DISTANCE(v,'[0,0]') AS d FROM v ORDER BY d LIMIT 10;")["rows"]
            assert actual[0] == [1, 0] and actual[1][0] == 2
            assert math.isclose(actual[1][1], distance, rel_tol=1e-5)
            assert client.ok("SELECT id FROM v WHERE g=2 ORDER BY L2_DISTANCE(v,'[0,0]') LIMIT 10;")["rows"] == [[2]]
            client.close()
            print(f"PASS indexed crash={name}: rows, postings, filtered and unfiltered TopK consistent")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--binary", required=True)
    p.add_argument("--temp-root")
    args = p.parse_args()
    run(args.binary, args.temp_root)
