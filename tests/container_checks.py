"""Linux amd64 /data, offline, SIGKILL, and above-memory tests.

Limits are LOCAL test conditions; they are not organizer resource limits.
"""
import argparse
import json
import subprocess
import time
import uuid

from client import Client, sql_string


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


def metrics(name):
    def cat(path):
        return docker("exec", name, "cat", "/sys/fs/cgroup/" + path)
    return {"memory_peak": int(cat("memory.peak")), "memory_max": cat("memory.max"),
            "swap_max": cat("memory.swap.max"), "io_stat": cat("io.stat")}


def run(image, large_rows):
    prefix = "mvdb-check-" + uuid.uuid4().hex[:10]
    volume, name = prefix + "-data", prefix
    docker("volume", "create", volume)
    command = ["docker", "run", "--rm", "--platform", "linux/amd64", "--network", "none",
               "--name", name, "--cpus", "2", "--memory", "96m", "--memory-swap", "96m",
               "-i", "-v", volume + ":/data", image]
    client = None
    start = time.perf_counter()
    try:
        client = Client(command, "/data", timeout=180)
        assert docker("exec", name, "uname", "-m") == "x86_64"
        client.ok("CREATE TABLE t(id BIGINT PRIMARY KEY,g BIGINT,s VARCHAR(3072),v VECTOR(128));")
        client.ok("BEGIN;")
        # 3,616+ logical bytes per row; the committed file must exceed 96 MiB.
        text = "界" * 1000
        vector = sql_string("[" + ",".join(["0"]*128) + "]")
        batch = 500
        for first in range(0, large_rows, batch):
            rows = [f"({i},{i%4},{sql_string(text)},{vector})"
                    for i in range(first, min(first+batch, large_rows))]
            client.ok("INSERT INTO t VALUES " + ",".join(rows) + ";")
        client.ok("COMMIT;")
        data_bytes = int(docker("exec", name, "stat", "-c", "%s", "/data/data"))
        assert data_bytes > 96*1024*1024, (data_bytes, large_rows)
        client.ok("CREATE VECTOR INDEX tv ON t(v);")
        query = f"SELECT id FROM t WHERE g=2 ORDER BY L2_DISTANCE(v,{vector}) LIMIT 10;"
        assert client.ok(query)["rows"] == [[i] for i in range(2, 42, 4)]
        # Forces external runs: long string keys, many equal keys, pk tie order.
        result = client.ok("SELECT id FROM t ORDER BY s DESC LIMIT 12;")["rows"]
        assert result == [[i] for i in range(12)], result
        client.ok("BEGIN;")
        client.ok("UPDATE t SET s='changed',v='[" + ",".join(["1"]*128) + "]' WHERE g>=1;")
        client.ok("DELETE FROM t WHERE g=0;")
        client.ok("ROLLBACK;")
        assert client.ok(query)["rows"] == [[i] for i in range(2, 42, 4)]
        client.ok("BEGIN;")
        client.ok("DELETE FROM t WHERE g=3;")
        client.ok("COMMIT;")
        client.ok("BEGIN;")
        client.ok("DELETE FROM t;")
        peak_before = metrics(name)
        docker("kill", "--signal", "KILL", name)
        client.process.wait(timeout=30)
        client.kill()
        client = Client(command, "/data", timeout=180)
        assert client.ok("SELECT id FROM t WHERE g=3 LIMIT 1;")["rows"] == []
        assert client.ok(query)["rows"] == [[i] for i in range(2, 42, 4)]
        count = len(client.ok("SELECT id FROM t;")["rows"])
        assert count == large_rows - large_rows//4, (count, large_rows)
        assert metrics(name)["swap_max"] == "0"
        peak_after = metrics(name)
        client.close()
        client = None
        report = {"status": "PASS", "platform": "linux/amd64", "network": "none",
                  "local_memory_limit": 96*1024*1024, "swap": 0, "rows": large_rows,
                  "data_bytes_before_index": data_bytes,
                  "phases": ["large_transaction_commit", "index_build", "filtered_topk",
                             "external_sort", "large_update_delete_rollback", "delete_commit",
                             "uncommitted_delete_sigkill", "restart"],
                  "before_kill": peak_before, "after_restart": peak_after,
                  "seconds": time.perf_counter()-start,
                  "io_caveat": "Raw cgroup io.stat; dedicated device/writeback attribution not certified."}
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return report
    finally:
        if client and client.process.poll() is None:
            subprocess.run(["docker", "kill", name], capture_output=True)
            client.kill()
        subprocess.run(["docker", "volume", "rm", volume], capture_output=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--image", default="minivectordb:local")
    p.add_argument("--rows", type=int, default=40000)
    p.add_argument("--output")
    args = p.parse_args()
    report = run(args.image, args.rows)
    if args.output:
        with open(args.output, "w") as f:
            json.dump(report, f, indent=2)
