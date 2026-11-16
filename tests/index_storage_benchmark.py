"""Paired indexed-before-insert, restart-first-query and large-K measurements."""
import argparse
import json
import math
from pathlib import Path
import re
import statistics
import subprocess
import sys
import time
import uuid

from benchmark import workload
from client import Client, sql_string
from container_checks import docker, metrics


def identity(image):
    info = json.loads(docker("image", "inspect", image))[0]
    digest = docker("run", "--rm", "--platform", "linux/amd64", "--network", "none",
                    "--entrypoint", "/usr/bin/sha256sum", image,
                    "/usr/local/bin/minivectordb").split()[0]
    return {"image": image, "image_id": info["Id"], "binary_sha256": digest}


def measure(image, batches, queries, truth, records):
    name = "mvdb-p0-index-" + uuid.uuid4().hex[:12]
    volume = name + "-data"
    docker("volume", "create", volume)
    command = ["docker", "run", "--rm", "--platform", "linux/amd64", "--network", "none",
               "--cpus", "2", "--memory", "128m", "--memory-swap", "128m", "--name", name,
               "-i", "-v", volume + ":/data", image]
    client = None
    try:
        client = Client(command, "/data", timeout=180)
        client.ok("CREATE TABLE t(id BIGINT PRIMARY KEY,g BIGINT,s VARCHAR(32),v VECTOR(128));")
        client.ok("CREATE VECTOR INDEX tv ON t(v);")
        before_write = metrics(name)
        started = time.perf_counter()
        for batch in batches:
            assert client.ok(batch)["affected_rows"] == 100
        write_ms = (time.perf_counter() - started) * 1000
        after_write = metrics(name)
        data_bytes = int(docker("exec", name, "stat", "-c", "%s", "/data/data"))
        client.close()
        startup = time.perf_counter()
        client = Client(command, "/data", timeout=180)
        startup_ms = (time.perf_counter() - startup) * 1000

        def check_topk(actual, expected):
            assert [r[0] for r in actual] == [i for _, i in expected]
            for (_, actual_d), (d, _) in zip(actual, expected):
                assert math.isclose(actual_d, d, rel_tol=1e-5, abs_tol=1e-5)

        before_first = metrics(name)
        started = time.perf_counter()
        actual = client.ok(queries[0])["rows"]
        first_ms = (time.perf_counter() - started) * 1000
        check_topk(actual, truth[0])
        after_first = metrics(name)
        for query in queries[:10]:
            client.ok(query)
        latencies = []
        for query, expected in zip(queries, truth):
            started = time.perf_counter()
            actual = client.ok(query)["rows"]
            latencies.append((time.perf_counter() - started) * 1000)
            check_topk(actual, expected)
        after_steady = metrics(name)
        large_k_ms = {"asc": [], "desc": []}
        zeros = sql_string(json.dumps([0] * 128))
        distances = [math.dist(v, [0] * 128) for v in records]
        for direction in ("asc", "desc"):
            expected = sorted(range(len(records)), key=lambda i:
                              ((-1 if direction == "desc" else 1) * distances[i], i))
            for _ in range(3):
                started = time.perf_counter()
                actual = client.ok(f"SELECT id,L2_DISTANCE(v,{zeros}) AS d "
                                   f"FROM t ORDER BY d {direction} LIMIT 20000;")["rows"]
                large_k_ms[direction].append((time.perf_counter() - started) * 1000)
                assert [r[0] for r in actual] == expected
                for i, d in actual:
                    assert math.isclose(d, distances[i], rel_tol=1e-5, abs_tol=1e-5)
        after_large_k = metrics(name)
        stored = client.ok("SELECT v FROM t ORDER BY id;")["rows"]
        assert [r[0] for r in stored] == records
        client.close()
        client = None
        return {"write_ms": write_ms, "data_bytes": data_bytes,
                "write_before": before_write, "write_after": after_write,
                "startup_including_docker_ms": startup_ms, "first_query_ms": first_ms,
                "query_before": before_first, "first_query_after": after_first,
                "steady_query_latencies_ms": latencies,
                "steady_filtered_median_ms": statistics.median(latencies[1::2]),
                "steady_unfiltered_median_ms": statistics.median(latencies[0::2]),
                "steady_query_after": after_steady,
                "large_k_ms": large_k_ms, "large_k_after": after_large_k,
                "exact_order_distance_and_restart_vector_check": True}
    finally:
        if client:
            if client.process.poll() is None:
                client.close()
        subprocess.run(["docker", "volume", "rm", volume], capture_output=True)


def run(args):
    report = {"metadata": {
        "command": sys.argv, "scope": "uninstrumented paired secondary checks, not official scoring",
        "platform": "Apple Silicon emulating Linux amd64; host load/cache not isolated",
        "rows": 10000, "dimension": 128, "steady_queries": 40, "K": 10,
        "index_created_before_insert": True, "write_batch": 100,
        "cpus": 2, "memory_limit": 134217728, "swap": 0,
        "first_query": "fresh process/cgroup, existing Linux volume; host page cache not evicted",
        "large_k": "LIMIT 20000, no filter, 3 ASC then 3 DESC queries; 10000 returned rows",
        "metrics": "raw cumulative cgroup peak and IO at phase boundaries; process RSS not measured",
        "caveats": "peak differences are not phase-local peaks; IO may include writeback and shared devices"},
        "baseline": {**identity(args.baseline), "rounds": []},
        "candidate": {**identity(args.candidate), "rounds": []}}
    output = Path(args.output)
    for n, seed in enumerate((20261116, 7, 39034843)):
        batches, queries, truth = workload(seed, 10000, 128, 40)
        records = [json.loads(text) for batch in batches
                   for text in re.findall(r"'(\[[^']+\])'", batch)]
        order = ("baseline", "candidate") if n != 1 else ("candidate", "baseline")
        for label in order:
            data = measure(report[label]["image"], batches, queries, truth, records)
            report[label]["rounds"].append({"seed": seed, **data})
            output.write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps({"seed": seed, "label": label, "write_ms": data["write_ms"],
                              "first_query_ms": data["first_query_ms"],
                              "large_k_median_ms": {k: statistics.median(v)
                                                    for k, v in data["large_k_ms"].items()}}),
                  flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--output", required=True)
    run(parser.parse_args())
