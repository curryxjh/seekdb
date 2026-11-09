"""Reproducible LOCAL comparison. No official E or ranking is computed.

Independent Python float32 + math.dist truth is prepared outside DB containers.
Each write/query item uses an isolated container/cgroup and a fresh named volume.
"""
import argparse
import hashlib
import heapq
import json
import math
from pathlib import Path
import random
import struct
import subprocess
import time
import uuid

from client import Client, sql_string
from container_checks import docker, metrics


def source_digest():
    h = hashlib.sha256()
    for path in sorted(Path("minivectordb").glob("*")):
        h.update(path.name.encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def workload(seed, count, dim, query_count):
    rng = random.Random(seed)
    f32 = lambda x: struct.unpack("<f", struct.pack("<f", x))[0]
    records = [[f32(rng.gauss(0, 1)) for _ in range(dim)] for _ in range(count)]
    batches = []
    for start in range(0, count, 100):
        values = [f"({i},{i%4},'row-{i}',{sql_string(json.dumps(records[i], separators=(',', ':')))})"
                  for i in range(start, min(start+100, count))]
        batches.append("INSERT INTO t VALUES " + ",".join(values) + ";")
    queries, truth = [], []
    for i in range(query_count):
        q = [f32(rng.gauss(0, 1)) for _ in range(dim)]
        group = None if i % 2 == 0 else i % 4
        where = "" if group is None else f" WHERE g={group}"
        queries.append(f"SELECT id,L2_DISTANCE(v,{sql_string(json.dumps(q, separators=(',', ':')))}) AS d"
                       f" FROM t{where} ORDER BY d LIMIT 10;")
        candidates = ((math.dist(v, q), key) for key, v in enumerate(records)
                      if group is None or key % 4 == group)
        truth.append(heapq.nsmallest(10, candidates))
    return batches, queries, truth


def item(image, label, batches, queries, truth, count, dim, indexed):
    name = "mvdb-bench-" + uuid.uuid4().hex[:10]
    volume = name + "-data"
    docker("volume", "create", volume)
    command = ["docker", "run", "--rm", "--platform", "linux/amd64", "--network", "none",
               "--name", name, "--cpus", "2", "--memory", "128m", "--memory-swap", "128m",
               "-i", "-v", volume + ":/data", image]
    client = None
    try:
        client = Client(command, "/data", timeout=120)
        client.ok(f"CREATE TABLE t(id BIGINT PRIMARY KEY,g BIGINT,s VARCHAR(32),v VECTOR({dim}));")
        start = time.perf_counter()
        for sql in batches:
            result = client.ok(sql)
            assert result["affected_rows"] == 100
        write_ms = (time.perf_counter()-start)*1000
        if label == "write":
            measured = metrics(name)
            client.close()
            client = Client(command, "/data", timeout=120)
            rows = client.ok("SELECT id,g,s FROM t ORDER BY id;")["rows"]
            assert rows == [[i, i%4, f"row-{i}"] for i in range(count)]
            # Check stored vector content after restart, outside measurement.
            stored = client.ok("SELECT v FROM t ORDER BY id;")["rows"]
            expected = []
            for sql in batches:
                # Parse only our own generated vector literals, independently
                # of the C++ parser; no DB logic is reused as truth.
                import re
                expected.extend(json.loads(x) for x in re.findall(r"'(\[[^']+\])'", sql))
            assert [row[0] for row in stored] == expected
            return {"T_ms": write_ms, **measured, "restart_content_check": True}
        preparation = time.perf_counter()
        if indexed:
            client.ok("CREATE VECTOR INDEX tv ON t(v);")
        preparation_ms = (time.perf_counter()-preparation)*1000
        for sql in queries[:10]:
            client.ok(sql)
        responses = []
        start = time.perf_counter()
        for sql in queries:
            responses.append(client.ok(sql)["rows"])
        query_ms = (time.perf_counter()-start)*1000
        measured = metrics(name)
        groups = [[], []]
        for i, (actual, expected) in enumerate(zip(responses, truth)):
            ids = [row[0] for row in actual]
            expected_ids = [key for dist, key in expected]
            assert len(ids) == len(expected_ids) == len(set(ids)), (i, ids)
            assert all(0 <= key < count for key in ids)
            if i % 2:
                assert all(key % 4 == i % 4 for key in ids)
            assert ids == expected_ids, (i, ids, expected_ids)
            for (_, actual_dist), (expected_dist, _) in zip(actual, expected):
                assert math.isclose(actual_dist, expected_dist, rel_tol=1e-5, abs_tol=1e-5)
            groups[i % 2].append(len(set(ids) & set(expected_ids))/len(expected_ids))
        return {"T_ms": query_ms, **measured, "preparation_ms": preparation_ms,
                "recall_unfiltered": sum(groups[0])/len(groups[0]),
                "recall_filtered": sum(groups[1])/len(groups[1]), "all_query_assertions": True}
    finally:
        if client and client.process.poll() is None:
            client.close()
        subprocess.run(["docker", "volume", "rm", volume], capture_output=True)


def run(args):
    image_id = docker("image", "inspect", args.image, "--format", "{{.Id}}")
    result = {"scope": "LOCAL comparison; no organizer baseline or official E",
              "source_sha256": source_digest(), "image_id": image_id,
              "platform": "Linux x86_64 emulated on Apple arm64 Docker Desktop",
              "cpus": 2, "memory_limit": 128*1024*1024, "swap": 0,
              "rows": args.rows, "dimension": args.dimension, "queries": args.queries,
              "K": 10, "write_batch": 100, "indexed": args.indexed,
              "cache_policy": "fresh cgroups/volumes, no host cache eviction; 10 warmup queries",
              "B_caveat": "Raw io.stat retained; dedicated data-device/writeback attribution unverified",
              "rounds": []}
    for seed in [20261109, 7, 39034843]:
        batches, queries, truth = workload(seed, args.rows, args.dimension, args.queries)
        write = item(args.image, "write", batches, queries, truth, args.rows, args.dimension, args.indexed)
        query = item(args.image, "query", batches, queries, truth, args.rows, args.dimension, args.indexed)
        round_ = {"seed": seed, "write": write, "query": query}
        result["rounds"].append(round_)
        print(json.dumps(round_), flush=True)
        Path(args.output).write_text(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--image", default="minivectordb:local")
    p.add_argument("--rows", type=int, default=10000)
    p.add_argument("--dimension", type=int, default=128)
    p.add_argument("--queries", type=int, default=1000)
    p.add_argument("--indexed", action="store_true")
    p.add_argument("--output", required=True)
    run(p.parse_args())
