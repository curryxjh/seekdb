"""Paired cold/early/warm exact-search measurements, including cache rebuilds.

Native mode needs root/systemd; Docker mode needs the production images. The
driver and independent float32/math.dist oracle remain outside the DB cgroup.
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
from container_checks import docker, metrics as docker_metrics
from system_cgroup_benchmark import command, metrics as native_metrics


def check(actual, expected):
    assert [r[0] for r in actual] == [key for _, key in expected], (actual, expected)
    for (_, distance), (wanted, _) in zip(actual, expected):
        assert math.isclose(distance, wanted, rel_tol=1e-5, abs_tol=1e-5)


def measure(target, batches, queries, truth, args):
    name = "mvdb-cold-" + uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix="mvdb-cold-") as directory:
        volume = name + "-data"
        if args.backend == "docker":
            docker("volume", "create", volume)
            directory = "/data"
        client = None

        def start(suffix):
            if args.backend == "native":
                return Client(command(target, name + suffix, "128M"), directory, timeout=240)
            return Client(["docker", "run", "--rm", "--platform", "linux/amd64",
                           "--network", "none", "--name", name + suffix, "--cpus", "2",
                           "--memory", "128m", "--memory-swap", "128m", "-i",
                           "-v", volume + ":/data", target], directory, timeout=240)

        def phase(suffix):
            times = []
            for sql, expected in zip(queries[:20], truth[:20]):
                begin = time.perf_counter()
                result = client.ok(sql)["rows"]
                times.append((time.perf_counter() - begin) * 1000)
                check(result, expected)
            # Check correctness outside the aggregate warm timer.
            begin = time.perf_counter()
            results = [client.ok(sql)["rows"] for sql in queries]
            warm = (time.perf_counter() - begin) * 1000
            for actual, expected in zip(results, truth):
                check(actual, expected)
            resource = (native_metrics(name + suffix) if args.backend == "native"
                        else docker_metrics(name + suffix))
            return {"first_query_ms": times[0], "first20_ms": times,
                    "first5_total_ms": sum(times[:5]), "first20_total_ms": sum(times),
                    "warm_queries": len(queries), "warm_total_ms": warm,
                    "exact_queries_checked": len(times) + len(results), "resource": resource}

        try:
            client = start("-insert")
            client.ok(f"CREATE TABLE t(id BIGINT PRIMARY KEY,g BIGINT,s VARCHAR(32),v VECTOR({args.dimension}));")
            begin = time.perf_counter()
            for sql in batches:
                assert client.ok(sql)["affected_rows"] == 100
            result = {"insert_ms": (time.perf_counter() - begin) * 1000}
            if args.indexed:
                client.ok("CREATE VECTOR INDEX tv ON t(v);")
            result["after_insert"] = phase("-insert")
            client.close()
            client = start("-restart")
            result["after_restart"] = phase("-restart")
            # A real committed row change invalidates the cache. Generated
            # filtered queries select groups 1/3, so their oracle stays valid.
            assert client.ok("UPDATE t SET g=7 WHERE id=0;")["affected_rows"] == 1
            result["after_mutation"] = phase("-restart")
            assert client.ok("SELECT id,g,s FROM t ORDER BY id;")["rows"] == [
                [i, 7 if i == 0 else i % 4, f"row-{i}"] for i in range(args.rows)]
            result["restart_content_check"] = True
            result["recall_filtered"] = result["recall_unfiltered"] = 1
            client.close()
            client = None
            return result
        finally:
            if client and client.process.poll() is None:
                client.kill()
                if args.backend == "docker":
                    subprocess.run(["docker", "rm", "-f", name + "-insert", name + "-restart"],
                                   capture_output=True)
            if args.backend == "docker":
                subprocess.run(["docker", "volume", "rm", volume], capture_output=True)


def run(args):
    assert args.rows % 100 == 0 and args.rows > 0 and args.queries >= 20
    targets = {label: getattr(args, label) for label in ["original", "baseline", "candidate"]
               if getattr(args, label)}
    identity = {}
    for label, target in targets.items():
        if args.backend == "native":
            targets[label] = str(Path(target).resolve())
            identity[label] = {"binary_sha256": hashlib.sha256(Path(target).read_bytes()).hexdigest()}
        else:
            identity[label] = {"image_id": docker("image", "inspect", target, "--format", "{{.Id}}")}
    report = {"scope": "LOCAL paired timing/correctness; not official E or platform score",
              "backend": args.backend, "platform": platform.platform(),
              "source_sha256": source_digest(), "rows": args.rows, "dimension": args.dimension,
              "queries": args.queries, "indexed": args.indexed, "K": 10,
              "memory_limit_bytes": 128 * 1024 * 1024, "swap_max": 0, "cpus": 2,
              "identity": identity, "rounds": [],
              "measurement": "Fresh DB/cgroup per version/seed; rotated order. Cold means empty process-local screening cache, not forced host-page-cache eviction. Every first query, following 19 queries, warm batch, restart and committed-mutation rebuild timed. Warm checking outside timer; per-query early timings include JSON transport, exclude oracle checks. Raw memory/IO counters cumulative within each DB process; shared WSL device attribution not certified."}
    for number, seed in enumerate([20261114, 8675309, 14223819]):
        batches, queries, truth = workload(seed, args.rows, args.dimension, args.queries)
        names = list(targets)
        order = names[number:] + names[:number]
        pair = {"seed": seed, "order": order}
        for name in order:
            pair[name] = measure(targets[name], batches, queries, truth, args)
        report["rounds"].append(pair)
        report["median_paired_speedups"] = {
            base: {phase: {metric: statistics.median(r[base][phase][metric] / r["candidate"][phase][metric]
                                                    for r in report["rounds"])
                           for metric in ["first_query_ms", "first5_total_ms", "first20_total_ms", "warm_total_ms"]}
                   for phase in ["after_insert", "after_restart", "after_mutation"]}
            for base in targets if base != "candidate"}
        Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"seed": seed, "order": order, "median_paired_speedups": report["median_paired_speedups"]}), flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(__doc__)
    p.add_argument("--backend", choices=["native", "docker"], default="native")
    p.add_argument("--original")
    p.add_argument("--baseline", required=True)
    p.add_argument("--candidate", required=True)
    p.add_argument("--rows", type=int, default=50000)
    p.add_argument("--dimension", type=int, default=128)
    p.add_argument("--queries", type=int, default=200)
    p.add_argument("--indexed", action="store_true")
    p.add_argument("--output", required=True)
    run(p.parse_args())
