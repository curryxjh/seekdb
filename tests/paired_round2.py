"""Current origin baseline vs candidate: adjacent three-round Linux T/B/M.

Separate from archived paired_benchmark.py, which preserves historical results.
"""
import argparse
import json
from pathlib import Path
import statistics
import subprocess

from benchmark import workload, item, source_digest
from compare import compare


def run(args):
    common = {"scope": "LOCAL cgroup comparison, not official E or score",
              "platform": "Linux x86_64 emulated on Apple arm64 Docker Desktop",
              "cpus": 2, "memory_limit": 128*1024*1024, "swap": 0,
              "rows": 10000, "dimension": 128, "queries": args.queries,
              "K": 10, "write_batch": 100, "indexed": False,
              "cache_policy": "fresh cgroups/volumes, no host cache eviction; 10 warmup queries",
              "B_caveat": "Raw io.stat; dedicated device/writeback attribution unverified",
              "timing_note": "Adjacent pairs with reversed order in middle round; no simultaneous Agent build/test"}
    reports = {}
    for label, image in [("baseline", args.baseline), ("candidate", args.candidate)]:
        reports[label] = {**common, "image": image, "rounds": [],
                          "image_id": subprocess.check_output(["docker", "image", "inspect", image,
                                                                "--format", "{{.Id}}"], text=True).strip()}
    reports["candidate"]["source_sha256"] = source_digest()
    reports["baseline"]["source_commit"] = subprocess.check_output(
        ["git", "rev-parse", "--verify", args.baseline_commit + "^{commit}"], text=True).strip()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    for number, seed in enumerate([20261112, 39034843, 774193]):
        b, q, truth = workload(seed, 10000, 128, args.queries)
        order = ["baseline", "candidate"] if number % 2 == 0 else ["candidate", "baseline"]
        for label in order:
            report = reports[label]
            result = {"seed": seed,
                      "write": item(report["image"], "write", b, q, truth, 10000, 128, False),
                      "query": item(report["image"], "query", b, q, truth, 10000, 128, False)}
            report["rounds"].append(result)
            (output / (label + ".json")).write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps({"label": label, **result}), flush=True)
    result = compare(reports["baseline"], reports["candidate"])
    pairs = {r["seed"]: r for r in reports["candidate"]["rounds"]}
    result["median_paired_query_speedup"] = statistics.median(
        r["query"]["T_ms"] / pairs[r["seed"]]["query"]["T_ms"]
        for r in reports["baseline"]["rounds"])
    (output / "comparison.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"median_paired_query_speedup": result["median_paired_query_speedup"],
                      "median_local_proxy": result["median_local_proxy"]}), flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--baseline", default="minivectordb:round2-origin")
    p.add_argument("--candidate", default="minivectordb:round2-final")
    p.add_argument("--baseline-commit", default="12b855468")
    p.add_argument("--queries", type=int, default=1000)
    p.add_argument("--output", default="docs/results/round2-final")
    run(p.parse_args())
