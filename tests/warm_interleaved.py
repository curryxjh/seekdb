"""LOCAL interleaved per-query comparison, independent exact oracle outside timers."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import statistics
import tempfile
import time
import uuid
from contextlib import ExitStack

from benchmark import workload, source_digest
from client import Client
from container_checks import docker, metrics


def check(actual, expected):
    assert [r[0] for r in actual] == [key for _, key in expected], (actual, expected)
    for (_, distance), (wanted, _) in zip(actual, expected):
        assert math.isclose(distance, wanted, rel_tol=1e-5, abs_tol=1e-5)


def run(args):
    targets = {label: (str(Path(target).resolve()) if args.backend == 'native' else target) for label, target in
               (item.split('=', 1) for item in args.target)}
    report = {'scope': 'LOCAL interleaved exact timing; not platform E', 'backend': args.backend,
              'source_sha256': source_digest(), 'rows': args.rows, 'dimension': args.dimension,
              'unique_queries_per_seed': args.queries, 'repeats': args.repeats,
              'policy': 'Populate all DBs before timing; rotate per-query version order; check every result outside timer. Cold is process cache cold, no page-cache eviction.',
              'identity': {k: ({'binary_sha256': hashlib.sha256(Path(v).read_bytes()).hexdigest()}
                              if args.backend == 'native' else
                              {'image_id': docker('image', 'inspect', v, '--format', '{{.Id}}')})
                           for k, v in targets.items()}, 'rounds': []}
    for number, seed in enumerate([20261114, 8675309, 14223819]):
        batches, queries, truth = workload(seed, args.rows, args.dimension, args.queries)
        clients = {}
        pair = {'seed': seed, 'cold_ms': {}, 'insert_ms': {}, 'resources': {},
                'timings_ms': {k: [] for k in targets}}
        containers = {}
        with ExitStack() as stack:
            for label, binary in targets.items():
                if args.backend == 'native':
                    directory = stack.enter_context(tempfile.TemporaryDirectory(prefix='mvdb-warm-'))
                    command = [binary]
                else:
                    name = containers[label] = 'mvdb-interleaved-' + uuid.uuid4().hex[:12]
                    volume = name + '-data'
                    docker('volume', 'create', volume)
                    stack.callback(lambda v=volume: docker('volume', 'rm', v))
                    directory = '/data'
                    command = ['docker', 'run', '--rm', '--platform', 'linux/amd64', '--network', 'none',
                               '--cpus', '2', '--memory', '128m', '--memory-swap', '128m', '--name', name,
                               '-i', '-v', volume + ':/data', binary]
                c = clients[label] = Client(command, directory, timeout=240)
                stack.callback(c.close)
                c.ok(f'CREATE TABLE t(id BIGINT PRIMARY KEY,g BIGINT,s VARCHAR(32),v VECTOR({args.dimension}));')
                begin = time.perf_counter_ns()
                for sql in batches:
                    assert c.ok(sql)['affected_rows'] == 100
                pair['insert_ms'][label] = (time.perf_counter_ns() - begin) / 1e6
            names = list(targets)
            rotated = names[number:] + names[:number]
            for label in rotated:
                c = clients[label]
                begin = time.perf_counter_ns()
                actual = c.ok(queries[0])['rows']
                pair['cold_ms'][label] = (time.perf_counter_ns() - begin) / 1e6
                check(actual, truth[0])
                for sql, expected in zip(queries[:20], truth[:20]):
                    check(c.ok(sql)['rows'], expected)
            for repeat in range(args.repeats):
                for i, (sql, expected) in enumerate(zip(queries, truth)):
                    start = (i + repeat) % len(names)
                    for label in names[start:] + names[:start]:
                        begin = time.perf_counter_ns()
                        actual = clients[label].ok(sql)['rows']
                        pair['timings_ms'][label].append((time.perf_counter_ns() - begin) / 1e6)
                        check(actual, expected)
            if args.backend == 'docker':
                pair['resources'] = {label: metrics(name) for label, name in containers.items()}
        pair['total_ms'] = {k: sum(v) for k, v in pair['timings_ms'].items()}
        baseline = names[0]
        pair['speedup_vs_' + baseline] = {k: pair['total_ms'][baseline] / v
                                           for k, v in pair['total_ms'].items()}
        report['rounds'].append(pair)
        report['median_paired_speedup'] = {k: statistics.median(
            r['total_ms'][baseline] / r['total_ms'][k] for r in report['rounds']) for k in names}
        Path(args.output).write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps({k: v for k, v in pair.items() if k != 'timings_ms'}), flush=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(__doc__)
    p.add_argument('--target', action='append', required=True, help='label=binary; first is baseline')
    p.add_argument('--backend', choices=['native', 'docker'], default='native')
    p.add_argument('--rows', type=int, default=10000)
    p.add_argument('--dimension', type=int, default=128)
    p.add_argument('--queries', type=int, default=100)
    p.add_argument('--repeats', type=int, default=5)
    p.add_argument('--output', required=True)
    run(p.parse_args())
