"""Paired Linux service-cgroup timings/resources with independent exact truth.

Requires a user systemd manager with the memory and IO controllers delegated.
The oracle and Python driver stay outside the database cgroups. This is a local
diagnostic; it does not implement the organizer's score or replace image tests.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import platform
import statistics
import subprocess
import tempfile
import time
import uuid

from benchmark import workload, source_digest
from client import Client


def service_command(binary, unit, memory):
    return ["systemd-run", "--user", "--quiet", "--pipe", "--wait", "--collect",
            "--service-type=exec", "--unit=" + unit,
            "-p", "MemoryMax=" + memory, "-p", "MemorySwapMax=0",
            "-p", "CPUQuota=200%", "-p", "IOAccounting=yes", str(Path(binary).resolve())]


def metrics(unit):
    group = subprocess.check_output(
        ["systemctl", "--user", "show", unit, "--property=ControlGroup", "--value"],
        text=True).strip()
    root = Path("/sys/fs/cgroup") / group.lstrip("/")
    values = {key: (root / key).read_text().strip()
              for key in ["memory.peak", "memory.max", "memory.swap.max"]}
    values["io.stat"] = (root / "io.stat").read_text().strip() if (root / "io.stat").exists() else None
    values["proc_io"] = None
    for pid in (root / "cgroup.procs").read_text().split():
        try:
            lines = Path("/proc", pid, "io").read_text().splitlines()
        except (FileNotFoundError, PermissionError):
            continue
        values["proc_io"] = {key: int(value) for key, value in (line.split(":") for line in lines)}
        break
    values["memory.peak"] = int(values["memory.peak"])
    return values


def measure(binary, batches, queries, truth, rows, dimension, memory):
    unit = "mvdb-perf5000-" + uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix="mvdb-perf5000-") as directory:
        client = Client(service_command(binary, unit, memory), directory, timeout=180)
        try:
            client.ok(f"CREATE TABLE t(id BIGINT PRIMARY KEY,g BIGINT,s VARCHAR(32),v VECTOR({dimension}));")
            start = time.perf_counter()
            for sql in batches:
                assert client.ok(sql)["affected_rows"] == 100
            write = {"T_ms": (time.perf_counter() - start) * 1000, **metrics(unit)}
            cold_start = time.perf_counter()
            client.ok(queries[0])
            cold_query_ms = (time.perf_counter() - cold_start) * 1000
            for sql in queries[:10]:
                client.ok(sql)
            start = time.perf_counter()
            results = [client.ok(sql)["rows"] for sql in queries]
            query = {"T_ms": (time.perf_counter() - start) * 1000, **metrics(unit)}
            for index, (actual, expected) in enumerate(zip(results, truth)):
                assert [r[0] for r in actual] == [i for _, i in expected], (index, actual, expected)
                for (_, distance), (wanted, _) in zip(actual, expected):
                    assert math.isclose(distance, wanted, rel_tol=1e-5, abs_tol=1e-5)
            size = (Path(directory) / "data").stat().st_size
            client.close()
            client = Client(service_command(binary, unit + "-restart", memory), directory, timeout=180)
            stored = client.ok("SELECT id,g,s FROM t ORDER BY id;")["rows"]
            assert stored == [[i, i % 4, f"row-{i}"] for i in range(rows)]
            return {"write": write, "query": query, "cold_query_ms": cold_query_ms,
                    "data_bytes": size, "exact_queries": len(results),
                    "restart_content_check": True, "recall_filtered": 1, "recall_unfiltered": 1}
        finally:
            if client.process.poll() is None:
                client.close()


def run(args):
    binaries = {name: str(Path(path).resolve()) for name, path in
                [("baseline", args.baseline), ("candidate", args.candidate)]}
    report = {"scope": "LOCAL native paired T/B/M; no official score prediction",
              "platform": platform.platform(), "memory_limit": args.memory,
              "cpu_quota": "200%", "swap_max": 0, "rows": args.rows,
              "dimension": args.dimension, "queries": args.queries, "rounds": [],
              "candidate_source_sha256": source_digest(),
              "binaries": {name: {"path": path, "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()}
                           for name, path in binaries.items()},
              "measurement": "One fresh service cgroup per version/seed; query resources include setup and warmup. Python/oracle outside cgroup. io.stat is null if IO controller is not delegated; /proc/PID/io retained separately, not relabeled as cgroup IO. Neither is organizer certified. Order reverses in middle round."}
    for index, seed in enumerate([20261113, 8675309, 14223819]):
        batches, queries, truth = workload(seed, args.rows, args.dimension, args.queries)
        pair = {"seed": seed}
        order = list(binaries) if index % 2 == 0 else list(reversed(binaries))
        for name in order:
            pair[name] = measure(binaries[name], batches, queries, truth,
                                 args.rows, args.dimension, args.memory)
        pair["write_speedup"] = pair["baseline"]["write"]["T_ms"] / pair["candidate"]["write"]["T_ms"]
        pair["query_speedup"] = pair["baseline"]["query"]["T_ms"] / pair["candidate"]["query"]["T_ms"]
        report["rounds"].append(pair)
        for field in ["write_speedup", "query_speedup"]:
            report["median_paired_" + field] = statistics.median(r[field] for r in report["rounds"])
        Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(pair), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--rows", type=int, default=10000)
    parser.add_argument("--dimension", type=int, default=128)
    parser.add_argument("--queries", type=int, default=1000)
    parser.add_argument("--memory", default="128M")
    parser.add_argument("--output", required=True)
    run(parser.parse_args())
