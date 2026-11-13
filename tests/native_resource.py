"""Exercise real disk data larger than a native Linux service's 96 MiB budget.

Same database operations as container_checks.py; the systemd service supplies
the memory limit when Docker is unavailable. This does not test the image.
"""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

from client import Client, sql_string
from native_cgroup_benchmark import service_command, metrics


def run(binary):
    start = time.perf_counter()
    name = "mvdb-resource-" + uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix="mvdb-native-resource-") as directory:
        client = Client(service_command(binary, name, "96M"), directory, timeout=240)
        try:
            client.ok("CREATE TABLE t(id BIGINT PRIMARY KEY,g BIGINT,s VARCHAR(3072),v VECTOR(128));")
            client.ok("BEGIN;")
            text = sql_string("界" * 1000)
            vector = sql_string("[" + ",".join(["0"] * 128) + "]")
            for first in range(0, 40000, 500):
                values = [f"({i},{i%4},{text},{vector})" for i in range(first, first + 500)]
                client.ok("INSERT INTO t VALUES " + ",".join(values) + ";")
            client.ok("COMMIT;")
            data_bytes = (Path(directory) / "data").stat().st_size
            assert data_bytes > 96 * 1024 * 1024
            print("PASS native 96MiB large commit", data_bytes, flush=True)
            client.ok("CREATE VECTOR INDEX tv ON t(v);")
            query = f"SELECT id FROM t WHERE g=2 ORDER BY L2_DISTANCE(v,{vector}) LIMIT 10;"
            assert client.ok(query)["rows"] == [[i] for i in range(2, 42, 4)]
            assert client.ok("SELECT id FROM t ORDER BY s DESC LIMIT 12;")["rows"] == [[i] for i in range(12)]
            print("PASS native 96MiB index, cache, filtered TopK, external sort", flush=True)
            client.ok("BEGIN;")
            client.ok("UPDATE t SET s='changed',v='[" + ",".join(["1"] * 128) + "]' WHERE g>=1;")
            client.ok("DELETE FROM t WHERE g=0;")
            client.ok("ROLLBACK;")
            assert client.ok(query)["rows"] == [[i] for i in range(2, 42, 4)]
            client.ok("BEGIN;")
            client.ok("DELETE FROM t WHERE g=3;")
            client.ok("COMMIT;")
            client.ok("BEGIN;")
            client.ok("DELETE FROM t;")
            before = metrics(name)
            assert before["memory.max"] == str(96 * 1024 * 1024)
            assert before["memory.swap.max"] == "0"
            subprocess.run(["systemctl", "--user", "kill", "--signal=SIGKILL", "--kill-whom=main", name], check=True)
            client.process.wait(timeout=30)
            client.kill()
            client = Client(service_command(binary, name + "-restart", "96M"), directory, timeout=240)
            assert client.ok("SELECT id FROM t WHERE g=3 LIMIT 1;")["rows"] == []
            assert client.ok(query)["rows"] == [[i] for i in range(2, 42, 4)]
            assert len(client.ok("SELECT id FROM t;")["rows"]) == 30000
            after = metrics(name + "-restart")
            print("PASS native 96MiB mutation rollback, commit, SIGKILL and restart", flush=True)
            return {"passed": True, "scope": "local native cgroup resource check; not a container image test",
                    "memory_limit": 96 * 1024 * 1024, "swap_max": 0,
                    "rows": 40000, "data_bytes_before_index": data_bytes,
                    "before_kill": before, "after_restart": after,
                    "elapsed_s": time.perf_counter() - start}
        finally:
            if client.process.poll() is None:
                client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = run(str(Path(args.binary).resolve()))
    Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result), flush=True)
