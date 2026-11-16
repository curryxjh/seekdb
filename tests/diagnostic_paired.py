"""Adjacent uninstrumented cgroup rounds for isolated P0 prototypes."""
import argparse
import json
from pathlib import Path
import statistics
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from benchmark import workload, item


def identity(image):
    info = json.loads(subprocess.check_output(["docker", "image", "inspect", image], text=True))[0]
    sha = subprocess.check_output(["docker", "run", "--rm", "--platform", "linux/amd64",
           "--network", "none", "--entrypoint", "/usr/bin/sha256sum", image,
           "/usr/local/bin/minivectordb"], text=True).split()[0]
    return {"image": image, "image_id": info["Id"], "architecture": info["Architecture"],
            "binary_sha256": sha}


def run(args):
    reports = {name: {**identity(image), "rounds": []} for name, image in
               (("baseline", args.baseline), ("candidate", args.candidate))}
    reports["metadata"] = {
        "scope": args.scope, "baseline_commit": "70b9fd50907b34c14fb44d9adea4897558f40b8e",
        "platform": "Linux amd64 emulated on Apple Silicon; host load/cache not isolated",
        "cpus": 2, "memory_limit": 134217728, "swap": 0,
        "rows": 10000, "dimensions": 128, "queries": 1000, "K": 10,
        "write_batch": 100, "indexed": args.indexed, "warmup_queries": 10, "filter_query_fraction": 0.5,
        "filter_selectivity": 0.25,
        "measurement": "uninstrumented; wall request/response; cumulative cgroup io.stat and memory.peak",
        "caveats": ["query B/M include loading and warmup, not stage deltas",
                    "dedicated device/writeback attribution unverified", "not official platform score"],
        "command": sys.argv,
    }
    output = Path(args.output)
    for round_no, seed in enumerate((20261116, 7, 39034843)):
        batches, queries, truth = workload(seed, 10000, 128, 1000)
        order = ("baseline", "candidate") if round_no != 1 else ("candidate", "baseline")
        print(json.dumps({"start_seed": seed, "order": order}), flush=True)
        results = {}
        for name in order:
            image = reports[name]["image"]
            write = item(image, "write", batches, queries, truth, 10000, 128, args.indexed)
            query = item(image, "query", batches, queries, truth, 10000, 128, args.indexed)
            results[name] = {"write": write, "query": query}
            reports[name]["rounds"].append({"seed": seed, **results[name]})
            output.write_text(json.dumps(reports, indent=2) + "\n")
        pair = {"seed": seed, "write_T_ratio_old_over_new":
                results["baseline"]["write"]["T_ms"] / results["candidate"]["write"]["T_ms"],
                "query_T_ratio_old_over_new":
                results["baseline"]["query"]["T_ms"] / results["candidate"]["query"]["T_ms"]}
        print(json.dumps(pair), flush=True)
    output.write_text(json.dumps(reports, indent=2) + "\n")
    for mode in ("write", "query"):
        ratios = []
        for index in range(3):
            ratios.append(reports["baseline"]["rounds"][index][mode]["T_ms"] /
                          reports["candidate"]["rounds"][index][mode]["T_ms"])
        print(mode, "paired median speedup", statistics.median(ratios), flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--baseline", default="minivectordb:perf5000-70b9")
    p.add_argument("--candidate", default="minivectordb:perf5000-writebatch")
    p.add_argument("--output", required=True)
    p.add_argument("--indexed", action="store_true")
    p.add_argument("--scope", default="A/B isolated P0 prototype; no diagnostic instrumentation")
    run(p.parse_args())
