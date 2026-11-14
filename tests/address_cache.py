"""Exact checks across coded, address-only and uncached regions, then root reuse."""
import argparse
import json
from pathlib import Path
import tempfile

from client import Client, sql_string


def run(binary):
    # At the maximum 8 MiB cache, 170k rows exceed both prefix boundaries.
    count, dimension = 170000, 32
    checks = 0
    with tempfile.TemporaryDirectory(prefix='mvdb-address-') as directory:
        c = Client([binary], directory, timeout=240)
        records = {}
        try:
            # Variable UTF-8 strings before the vector prevent fixed-position
            # address assumptions. Negative/non-contiguous primary keys matter.
            c.ok('CREATE TABLE t(s VARCHAR(30),v VECTOR(32),id BIGINT PRIMARY KEY,g BIGINT);')
            c.ok('BEGIN;')
            for start in range(0, count, 100):
                rows = []
                for i in range(start, min(start + 100, count)):
                    key = i * 3 - count
                    records[key] = i
                    text = '界' * (i % 7)
                    vector = sql_string(json.dumps([i] * dimension))
                    rows.append(f'({sql_string(text)},{vector},{key},{i%5})')
                c.ok('INSERT INTO t VALUES ' + ','.join(rows) + ';')
            c.ok('COMMIT;')

            def verify(target, limit=10, minimum=0, maximum=count, desc=False, group=None):
                nonlocal checks
                lo, hi = minimum * 3 - count, maximum * 3 - count
                where = f'id>={lo} AND id<{hi}'
                if group is not None:
                    where += f' AND g={group}'
                q = sql_string(json.dumps([target] * dimension))
                direction = ' DESC' if desc else ''
                actual = c.ok(f'SELECT id FROM t WHERE {where} ORDER BY L2_DISTANCE(v,{q})'
                              f'{direction} LIMIT {limit};')['rows']
                wanted = sorted((key for key, value in records.items()
                                 if lo <= key < hi and (group is None or value % 5 == group)),
                                key=lambda key: ((-1 if desc else 1) * abs(records[key] - target), key))[:limit]
                assert actual == [[key] for key in wanted], (target, limit, where, desc, actual[:20], wanted[:20])
                checks += 1

            for target in [100, 130000, 169977]:
                for desc in [False, True]:
                    verify(target, desc=desc)
                    verify(target, group=3, desc=desc)
            for limit in [1, 64, 65, 999]:
                # No coded row matches, plus a true uncached tree-scan tail.
                verify(169977, limit, minimum=169900)
                verify(169977, limit, minimum=169900, desc=True)
                # Matches addressed-but-uncoded rows only; no tree tail.
                verify(130000, limit, minimum=129900, maximum=130100)
            verify(100, group=99)
            for _ in range(2):
                verify(130000)
            victim = 130000 * 3 - count
            c.ok('BEGIN;')
            c.ok(f'DELETE FROM t WHERE id={victim};')
            saved = records.pop(victim)
            verify(130000)
            bad = c.sql('INSERT INTO t VALUES(\'new\',' + sql_string(json.dumps([130000] * dimension)) +
                        f',999999,0),(\'duplicate\',' + sql_string(json.dumps([1] * dimension)) +
                        f',{-count},0);')
            assert not bad['ok']
            verify(130000)
            c.ok('ROLLBACK;')
            records[victim] = saved
            verify(130000)
            c.ok(f'UPDATE t SET s=\'new-text\' WHERE id={victim};')
            verify(130000)
            c.close()
            c = Client([binary], directory, timeout=240)
            verify(169977)
            verify(130000, 65, minimum=129900, maximum=130100)
        finally:
            c.close()
    print(f'PASS address cache: {checks} exact checks; 170000x32, ASC/DESC, K=1/64/65/999, empty/uncoded/tail, UTF-8 offsets, failure/rollback/restart')


if __name__ == '__main__':
    p = argparse.ArgumentParser(__doc__)
    p.add_argument('--binary', required=True)
    run(str(Path(p.parse_args().binary).resolve()))
