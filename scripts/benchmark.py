"""Reproducible local smoke benchmark. These are not official cgroup scores."""
import argparse
import json
import math
import os
from pathlib import Path
import random
try:
    import resource
except ImportError:
    resource = None
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from minivectordb.engine import Database


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--rows', type=int, default=10000)
    parser.add_argument('--dim', type=int, default=128)
    parser.add_argument('--queries', type=int, default=1000)
    parser.add_argument('--index', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    rng = random.Random(20261111)
    with tempfile.TemporaryDirectory(prefix='minivector-benchmark-') as folder:
        db = Database(folder)
        db.execute(f'CREATE TABLE t (id BIGINT PRIMARY KEY, category BIGINT, v VECTOR({args.dim}));')
        # Data generation is outside the measured insert window.
        import struct
        vectors = [[struct.unpack('<f', struct.pack('<f', rng.gauss(0, 1)))[0] for _ in range(args.dim)] for _ in range(args.rows)]
        batches = []
        for start in range(0, args.rows, 100):
            batches.append('INSERT INTO t VALUES ' + ','.join(f"({i},{i%10},'{json.dumps(vectors[i])}')" for i in range(start, min(start+100, args.rows))) + ';')
        before = time.perf_counter()
        for batch in batches:
            db.execute(batch)
        write_seconds = time.perf_counter() - before
        before = time.perf_counter()
        if args.index:
            db.execute('CREATE VECTOR INDEX ix ON t(v);')
        db.close()
        db = Database(folder)
        recovery_seconds = time.perf_counter() - before
        queries, expected = [], []
        for i in range(args.queries):
            q = vectors[rng.randrange(args.rows)]
            where = f' WHERE category={i%10}' if i%2 else ''
            queries.append(f"SELECT id,L2_DISTANCE(v,'{json.dumps(q)}') AS d FROM t{where} ORDER BY d LIMIT 10;")
            if i < 10:
                candidates = range(args.rows) if not where else range(i%10, args.rows, 10)
                truth = sorted(((j, math.dist(vectors[j], q)) for j in candidates), key=lambda x: (x[1], x[0]))[:10]
                expected.append(truth)
        # Build the optional bounded cache and index during warmup.
        list(db.execute(queries[0])['rows'])
        before = time.perf_counter()
        results = []
        for i, query in enumerate(queries):
            result = list(db.execute(query)['rows'])
            if i < 10:
                results.append(result)
        query_seconds = time.perf_counter() - before
        for actual, wanted in zip(results, expected):
            assert [r[0] for r in actual] == [r[0] for r in wanted]
            assert all(math.isclose(a[1], b[1], abs_tol=1e-8, rel_tol=1e-8) for a,b in zip(actual, wanted))
        disk_bytes = (Path(folder) / 'commit.log').stat().st_size
        db.close()
        report = dict(rows=args.rows, dimensions=args.dim, queries=args.queries, index=args.index,
            write_seconds=write_seconds, query_seconds=query_seconds, reopen_seconds=recovery_seconds,
            commit_log_bytes=disk_bytes, harness_peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss if resource else None,
            verified_queries=len(expected), verified_recall=1.0,
            limitation='Local in-process wall time. RSS includes data generator and harness; no official cgroup I/O or baseline score. Only first 10 timed query results independently verified.')
        text = json.dumps(report, indent=2)
        print(text)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
