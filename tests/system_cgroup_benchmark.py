"""Three-way native Linux comparison with real system-service cgroup IO.

Run as root on a systemd host. Each binary/seed gets a transient service and a
fresh /tmp directory on Linux storage. The independent oracle is outside the DB
cgroup. This is local evidence, not an organizer E/score or a container test.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import statistics
import subprocess
import tempfile
import time
import uuid

from benchmark import workload, source_digest
from client import Client


def command(binary, unit, memory):
    return ["systemd-run", "--quiet", "--pipe", "--wait", "--collect",
            "--service-type=exec", "--unit=" + unit,
            "-p", "MemoryMax=" + memory, "-p", "MemorySwapMax=0",
            "-p", "CPUQuota=200%", "-p", "IOAccounting=yes", str(Path(binary).resolve())]


def metrics(unit):
    group = subprocess.check_output(
        ["systemctl", "show", unit, "--property=ControlGroup", "--value"], text=True).strip()
    root = Path("/sys/fs/cgroup") / group.lstrip("/")
    values = {key: (root / key).read_text().strip()
              for key in ["memory.peak", "memory.max", "memory.swap.max", "io.stat"]}
    values["memory.peak"] = int(values["memory.peak"])
    return values


def measure(binary, batches, queries, truth, args):
    unit = "mvdb-recovery-" + uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix="mvdb-recovery-") as directory:
        client = Client(command(binary, unit, args.memory), directory, timeout=240)
        try:
            client.ok(f"CREATE TABLE t(id BIGINT PRIMARY KEY,g BIGINT,s VARCHAR(32),v VECTOR({args.dimension}));")
            start = time.perf_counter()
            for sql in batches:
                assert client.ok(sql)["affected_rows"] == 100
            write = {"T_ms": (time.perf_counter() - start) * 1000, **metrics(unit)}
            index_start = time.perf_counter()
            if args.indexed:
                client.ok("CREATE VECTOR INDEX tv ON t(v);")
            index_ms = (time.perf_counter() - index_start) * 1000
            cold_start = time.perf_counter()
            client.ok(queries[0])
            cold_ms = (time.perf_counter() - cold_start) * 1000
            for sql in queries[:10]:
                client.ok(sql)
            start = time.perf_counter()
            results = [client.ok(sql)["rows"] for sql in queries]
            query = {"T_ms": (time.perf_counter() - start) * 1000, **metrics(unit)}
            for i, (actual, expected) in enumerate(zip(results, truth)):
                assert [r[0] for r in actual] == [key for _, key in expected], (i, actual, expected)
                for (_, distance), (wanted, _) in zip(actual, expected):
                    assert math.isclose(distance, wanted, rel_tol=1e-5, abs_tol=1e-5)
            size = (Path(directory) / "data").stat().st_size
            client.close()
            client = Client(command(binary, unit + "-restart", args.memory), directory, timeout=240)
            assert client.ok("SELECT id,g,s FROM t ORDER BY id;")["rows"] == [
                [i, i % 4, f"row-{i}"] for i in range(args.rows)]
            for sql, expected in zip(queries[:4], truth[:4]):
                assert [r[0] for r in client.ok(sql)["rows"]] == [key for _, key in expected]
            return {"write": write, "query": query, "cold_query_ms": cold_ms,
                    "index_ms": index_ms, "data_bytes": size, "exact_queries": len(results),
                    "restart_content_check": True, "recall_filtered": 1, "recall_unfiltered": 1}
        finally:
            if client.process.poll() is None:
                client.close()


def run(args):
    assert os.geteuid() == 0, "Needs system transient-service permission; use root"
    assert args.rows > 0 and args.rows % 100 == 0 and args.queries > 0
    binaries = {name: str(Path(getattr(args, name)).resolve())
                for name in ["original", "previous", "candidate"]}
    report = {"scope": "LOCAL system-service cgroup comparison, not official E/score or container validation",
              "platform": platform.platform(), "source_sha256": source_digest(),
              "rows": args.rows, "dimension": args.dimension, "queries": args.queries,
              "memory_limit": args.memory, "swap_max": 0, "cpu_quota": "200%",
              "indexed": args.indexed, "rounds": [],
              "measurement": "Fresh service cgroup and Linux /tmp directory per version/seed; oracle outside. Query resource counters include setup, index and warmup; T is warm timed queries. Cold T reported separately. No global cache eviction; shared host/WSL device attribution is not organizer certified. Rotated order across rounds. All commits included in timed write response.",
              "binaries": {name: {"path": path, "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()}
                           for name, path in binaries.items()}}
    for i, seed in enumerate([20261114, 8675309, 14223819]):
        batches, queries, truth = workload(seed, args.rows, args.dimension, args.queries)
        names = list(binaries)
        order = names[i:] + names[:i]
        pair = {"seed": seed, "order": order}
        for name in order:
            pair[name] = measure(binaries[name], batches, queries, truth, args)
        report["rounds"].append(pair)
        report["median_paired_speedups"] = {
            base: {phase: statistics.median(r[base][phase]["T_ms"] / r["candidate"][phase]["T_ms"]
                                            for r in report["rounds"])
                   for phase in ["write", "query"]} for base in ["original", "previous"]}
        Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(pair), flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(__doc__)
    for name in ["original", "previous", "candidate"]:
        p.add_argument("--" + name, required=True)
    p.add_argument("--rows", type=int, default=10000)
    p.add_argument("--dimension", type=int, default=128)
    p.add_argument("--queries", type=int, default=1000)
    p.add_argument("--memory", default="128M")
    p.add_argument("--indexed", action="store_true")
    p.add_argument("--output", required=True)
    run(p.parse_args())
