"""Adjacent native comparisons, exact independent truth; not an official score.

Linux cgroup T/B/M measurement is provided separately by benchmark.py. This
runner isolates query throughput and keeps baseline/candidate seeds identical.
"""
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import tempfile
import time

from benchmark import source_digest, workload
from client import Client


def measure(binary, batches, queries, truth, dimension):
    with tempfile.TemporaryDirectory(prefix="mvdb-round2-perf-") as directory:
        c = Client([binary], directory, timeout=120)
        try:
            c.ok(f"CREATE TABLE t(id BIGINT PRIMARY KEY,g BIGINT,s VARCHAR(32),v VECTOR({dimension}));")
            start = time.perf_counter()
            for sql in batches:
                assert c.ok(sql)["affected_rows"] == 100
            write_ms = (time.perf_counter() - start) * 1000
            for sql in queries[:10]:
                c.ok(sql)
            start = time.perf_counter()
            actual = [c.ok(sql)["rows"] for sql in queries]
            query_ms = (time.perf_counter() - start) * 1000
            # Same oracle and tolerance as the independent benchmark. Validation
            # is outside the timed region; ID/order equality implies recall=1.
            import math
            for i, (rows, expected) in enumerate(zip(actual, truth)):
                assert [r[0] for r in rows] == [key for _, key in expected], (i, rows, expected)
                for (_, dist), (wanted, _) in zip(rows, expected):
                    assert math.isclose(dist, wanted, rel_tol=1e-5, abs_tol=1e-5)
            return {"write_ms": write_ms, "query_ms": query_ms,
                    "exact_ID_order_distance_checks": len(actual),
                    "recall_unfiltered": 1.0, "recall_filtered": 1.0}
        finally:
            c.close()


def run(args):
    baseline, candidate = str(Path(args.baseline).resolve()), str(Path(args.candidate).resolve())
    report = {"scope": "LOCAL native end-to-end timings; no cgroup resources or official score",
              "source_sha256": source_digest(), "rows": args.rows, "dimensions": args.dimension,
              "queries": args.queries, "rounds": [],
              "binaries": {name: {"path": path, "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()}
                           for name, path in [("baseline", baseline), ("candidate", candidate)]}}
    for number, seed in enumerate([20261112, 39034843, 774193]):
        batches, queries, truth = workload(seed, args.rows, args.dimension, args.queries)
        pair = {"seed": seed}
        order = [("baseline", baseline), ("candidate", candidate)]
        if number % 2:
            order.reverse()
        for name, binary in order:
            pair[name] = measure(binary, batches, queries, truth, args.dimension)
        pair["query_speedup"] = pair["baseline"]["query_ms"] / pair["candidate"]["query_ms"]
        report["rounds"].append(pair)
        report["median_paired_query_speedup"] = statistics.median(r["query_speedup"] for r in report["rounds"])
        Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(pair), flush=True)
    print("median paired speedup:", report["median_paired_query_speedup"], flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--baseline", required=True)
    p.add_argument("--candidate", required=True)
    p.add_argument("--rows", type=int, default=10000)
    p.add_argument("--dimension", type=int, default=128)
    p.add_argument("--queries", type=int, default=1000)
    p.add_argument("--output", required=True)
    run(p.parse_args())
