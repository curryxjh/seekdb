"""Aggregate raw diagnostics without treating nested/sampled timers as additive."""
import argparse
from collections import Counter, defaultdict
import gzip
import json
import math
from pathlib import Path
import statistics

SPARSE = {"vector_convert", "row_build", "tree_find", "tree_put", "row_encode",
          "blob_write", "pwrite", "float_screen", "compensated", "final_heap", "vector_view"}


def load(path):
    return json.load(gzip.open(path, "rt")) if path.suffix == ".gz" else json.loads(path.read_text())


def latency(values):
    values = sorted(values)
    if not values:
        return None
    return {"n": len(values), "sum_ms": sum(values)/1e6, "mean_us": statistics.mean(values)/1e3,
            "p50_us": statistics.median(values)/1e3,
            "p95_us": values[math.ceil(.95*len(values))-1]/1e3,
            "p99_us": values[math.ceil(.99*len(values))-1]/1e3, "max_us": values[-1]/1e3}


def io(raw):
    result = {}
    for line in raw.splitlines():
        device, *fields = line.split()
        result[device] = {k: int(v) for k, v in (field.split("=") for field in fields)}
    return result


def phase_delta(before, after):
    first, last = io(before["io.stat"]), io(after["io.stat"])
    # Never sum distinct block devices; retain the raw per-device accounting.
    return {device: {key: value-first.get(device, {}).get(key, 0)
                     for key, value in fields.items()} for device, fields in last.items()}


def cpu_delta(before, after):
    def parse(snapshot):
        return {key: int(value) for key, value in
                (line.split() for line in snapshot["cpu.stat"].splitlines())}
    first, last = parse(before), parse(after)
    return {key: value-first.get(key, 0) for key, value in last.items()}


def check_counts(counts):
    """Identities specific to the fully coded TopK path used by this workload."""
    if "coded" not in counts:
        return
    assert counts["integer_checks"] + counts["seed_skipped"] == counts["coded"]
    assert counts["integer_reject"] + counts["integer_pass"] == counts["integer_checks"]
    assert counts["float_checks"] == counts["integer_pass"]
    assert counts["refines"] == counts["seed_pop"] + counts["float_checks"] - counts["float_reject"]
    assert counts["vector_accesses"] == counts["seed_pop"] + counts["integer_pass"]
    assert counts["final_fill"] + counts["final_replace"] <= counts["refines"]


def summarize(data):
    records = {r["sequence"]: r for r in data["diagnostic_records"]}
    phases = {}
    for phase, filtered in [("write", None), ("first", None), ("warmup", None),
                            ("steady", None), ("steady", False), ("steady", True),
                            ("point_probe", None)]:
        requests = [r for r in data["requests"] if r["phase"] == phase and
                    (filtered is None or r["filtered"] == filtered)]
        timers = defaultdict(Counter)
        counts = Counter()
        for request in requests:
            record = records.get(request["id"], {})
            for stage, slot in record.get("timers", {}).items():
                timers[stage].update(slot)
            counts.update(record.get("events", {}))
        check_counts(counts)
        result = {"latency": latency([r["wall_ns"] for r in requests]), "events": dict(counts), "timers": {}}
        wall = sum(r["wall_ns"] for r in requests)
        process = timers["request"]["ns"]
        for stage, slot in timers.items():
            if not slot["samples"]:
                continue
            ns = slot["ns"]
            if stage in SPARSE:
                ns *= slot["calls"]/slot["samples"]
            result["timers"][stage] = {
                **dict(slot), "estimate_ms": ns/1e6, "sampled": stage in SPARSE,
                "share_process_pct": ns/process*100 if process else None,
                "share_wall_pct": ns/wall*100 if wall else None,
                "note": "inclusive; sampled estimates include clock overhead" if stage in SPARSE else "inclusive"}
        if filtered is None and phase == "steady":
            result["external_unattributed_ms"] = (wall-process)/1e6 if process else None
        phases[phase + ("" if filtered is None else "_filtered" if filtered else "_unfiltered")] = result
    resources = data["phases"]
    return {
        "phases": phases,
        "container_start_to_ready_ms": data["container_start_to_ready_ms"],
        "restart_to_ready_ms": data.get("restart_to_ready_ms"),
        "resources": {
            "write_io_delta": phase_delta(resources["before_write"], resources["after_write"]),
            "first_io_delta": phase_delta(resources["after_write"], resources["after_first"]),
            "steady_io_delta": phase_delta(resources["before_steady"], resources["after_steady"]),
            "write_cpu_delta": cpu_delta(resources["before_write"], resources["after_write"]),
            "steady_cpu_delta": cpu_delta(resources["before_steady"], resources["after_steady"]),
            "ready_peak_bytes": int(resources["ready"]["memory.peak"]),
            "after_write_cumulative_peak_bytes": int(resources["after_write"]["memory.peak"]),
            "after_steady_cumulative_peak_bytes": int(resources["after_steady"]["memory.peak"]),
            "after_steady_process_status": resources["after_steady"]["processes"],
            "memory_events": resources["after_steady"]["memory.events"]},
        "response_sha256": data["response_sha256"],
        "restart_all_content_equal": data.get("restart_all_content_equal", False)}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("directories", nargs="+", type=Path)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()
    result = {"scope": "LOCAL diagnostic builds; no official E, no algorithm change",
              "sample_warning": "Nested inclusive times and fine estimates must not be summed. "
                                "Sparse projection: sum(ns)*calls/samples, no clock subtraction. "
                                "Write fine estimates disagree between strides 64/256 and are "
                                "not reliable for precise stage shares or ranking.",
              "runs": {}, "paired_deltas": {}}
    digests = set()
    for directory in args.directories:
        result["runs"][directory.name] = {}
        paired = defaultdict(dict)
        for file in sorted([*directory.glob("[0-9]*.json"), *directory.glob("[0-9]*.json.gz")]):
            data = load(file)
            summary = summarize(data)
            result["runs"][directory.name][data["label"]] = summary
            index, label = data["label"].split("-")
            paired[index][label] = summary
            digests.add(data["response_sha256"])
        result["paired_deltas"][directory.name] = []
        for index, pair in sorted(paired.items()):
            if not {"A", "B"} <= pair.keys():
                continue
            item = {"pair": index, "B_over_A": {}}
            for phase in ["write", "steady"]:
                a, b = [pair[label]["phases"][phase]["latency"]["sum_ms"] for label in ["A", "B"]]
                item["B_over_A"][phase] = b/a
            result["paired_deltas"][directory.name].append(item)
    assert len(digests) == 1, "exact JSON numeric results/order differ across measured executables"
    result["all_response_sha256"] = next(iter(digests))
    args.output.write_text(json.dumps(result, indent=2))
    print(json.dumps(result["paired_deltas"], indent=2))


if __name__ == "__main__":
    main()
