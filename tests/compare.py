"""LOCAL three-metric proxy; raw cgroup I/O is not certified organizer I/O."""
import argparse
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import math
from pathlib import Path
import statistics


def io_bytes(text):
    total = 0
    for line in text.splitlines():
        fields = dict(field.split("=", 1) for field in line.split()[1:])
        total += int(fields["rbytes"]) + int(fields["wbytes"])
    return total


def measure(item):
    return {"T_ms": item["T_ms"], "B_bytes": io_bytes(item["io_stat"]),
            "M_bytes": item["memory_peak"]}


def compare(baseline, candidate):
    for key in ["platform", "cpus", "memory_limit", "swap", "rows", "dimension",
                "queries", "K", "write_batch", "indexed", "cache_policy"]:
        assert baseline[key] == candidate[key], (key, baseline[key], candidate[key])
    seeds = [r["seed"] for r in baseline["rounds"]]
    assert len(seeds) == len(set(seeds)) == 3
    candidates = {r["seed"]: r for r in candidate["rounds"]}
    assert len(candidate["rounds"]) == 3 and set(candidates) == set(seeds)
    rounds = []
    for base in baseline["rounds"]:
        trial = candidates[base["seed"]]
        result = {"seed": base["seed"]}
        for phase in ["write", "query"]:
            for item in [base[phase], trial[phase]]:
                values = measure(item)
                assert all(math.isfinite(v) and v >= 0 for v in values.values())
                assert 0 < values["M_bytes"] <= candidate["memory_limit"]
                assert int(item["memory_max"]) == candidate["memory_limit"]
                assert int(item["swap_max"]) == 0
                if phase == "query":
                    assert item["all_query_assertions"]
                    assert item["recall_filtered"] >= 0.95
                    assert item["recall_unfiltered"] >= 0.95
                else:
                    assert item["restart_content_check"]
            b, c = measure(base[phase]), measure(trial[phase])
            weighted = 0
            ratios = {}
            for key, epsilon, weight in [
                    ("T_ms", 1, 0.6), ("B_bytes", 1024**2, 0.2),
                    ("M_bytes", 1024**2, 0.2)]:
                ratio = (b[key] + epsilon) / (c[key] + epsilon)
                ratios[key] = ratio
                weighted += weight * ratio / (1 + ratio)
            result[phase] = {"baseline": b, "candidate": c, "ratios": ratios,
                             "local_proxy": weighted}
        result["local_proxy"] = (result["write"]["local_proxy"]
                                 + 2 * result["query"]["local_proxy"]) / 3
        rounds.append(result)
    median = statistics.median(r["local_proxy"] for r in rounds)
    medians = {}
    for phase in ["write", "query"]:
        medians[phase] = {
            version: {key: statistics.median(r[phase][version][key] for r in rounds)
                      for key in ["T_ms", "B_bytes", "M_bytes"]}
            for version in ["baseline", "candidate"]}
    return {"scope": "LOCAL proxy only; no official baseline, E, score or ranking",
            "B_caveat": "Raw cgroup io.stat across reported devices; dedicated "
                        "device and writeback attribution are not certified",
            "formula": "0.6*time + 0.2*IO + 0.2*memory; query weight 2; "
                       "f(R)=R/(1+R); epsilon=1ms,1MiB,1MiB; per-round then median",
            "median_local_proxy": str(Decimal(str(median)).quantize(
                Decimal("0.000001"), rounding=ROUND_HALF_UP)),
            "no_change_reference": 0.5, "rounds": rounds,
            "metric_medians_for_description_only": medians}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--baseline", type=Path, required=True)
    p.add_argument("--candidate", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    result = compare(json.loads(args.baseline.read_text()),
                     json.loads(args.candidate.read_text()))
    result["inputs"] = [
        {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for path in [args.baseline, args.candidate]]
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result["metric_medians_for_description_only"], indent=2))
    print("LOCAL median proxy:", result["median_local_proxy"])
