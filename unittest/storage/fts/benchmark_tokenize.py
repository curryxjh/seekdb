#!/usr/bin/env python3
"""Diagnostic only: alternate long-document TOKENIZE on two Linux instances.

This does not replace or modify the official competition benchmark.
"""
import argparse
import json
import statistics
import time

from sql_differential import connect


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--user", default="root")
    parser.add_argument("--baseline-port", type=int, required=True)
    parser.add_argument("--candidate-port", type=int, required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    connections = {}
    report = {}
    try:
        for label in ("baseline", "candidate"):
            args.port = getattr(args, label + "_port")
            connections[label] = connect(args)
        workloads = {
            "beng_repeated": ("beng", "database performance audit repeated " * 2000),
            "beng_distinct": ("beng", " ".join("word%08d" % i for i in range(8000))),
            "ik_repeated": ("ik", "数据库全文索引中文检索 database performance " * 500),
        }
        for name, parameters in workloads.items():
            analyzer, text = parameters
            values = {label: [] for label in connections}
            expected = None
            for sample in range(7):
                # Alternate order to reduce drift; discard the first two groups.
                order = ("baseline", "candidate") if sample % 2 == 0 else ("candidate", "baseline")
                for label in order:
                    with connections[label].cursor() as cur:
                        start = time.perf_counter()
                        for _ in range(10):
                            cur.execute("SELECT JSON_LENGTH(TOKENIZE(%s,%s)) AS n", (text, analyzer))
                            count = cur.fetchone()[0]
                            if expected is None:
                                expected = count
                            if count != expected:
                                raise AssertionError("diagnostic token count changed")
                        elapsed = (time.perf_counter() - start) * 1000 / 10
                        if sample >= 2:
                            values[label].append(elapsed)
            medians = {label: statistics.median(items) for label, items in values.items()}
            report[name] = {"samples_ms": values, "median_ms": medians,
                            "reduction": 1 - medians["candidate"] / medians["baseline"]}
        with open(args.output, "w") as output:
            json.dump(report, output, indent=2)
        print(json.dumps(report, indent=2))
    finally:
        for connection in connections.values():
            connection.close()


if __name__ == "__main__":
    main()
