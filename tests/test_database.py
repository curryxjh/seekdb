"""Public cases plus independent adversarial, differential and recovery checks.

Run: python -m unittest discover -s tests -v
"""
import contextlib
import json
import math
import os
from pathlib import Path
import queue
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'protocol_check'))
from _assertions import assert_response
from minivectordb.engine import Database
from minivectordb.sql import DBError
from minivectordb.storage import FRAME, TRAILER


class Client:
    def __init__(self, data):
        self.data, self.seq = str(data), 0
        self.start()

    def start(self):
        self.messages = queue.Queue()
        self.stderr = tempfile.TemporaryFile()
        self.proc = subprocess.Popen([sys.executable, '-m', 'minivectordb', '--data-dir', self.data], cwd=ROOT,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr,
            text=True, encoding='utf-8', env=dict(os.environ, PYTHONUTF8='1', OPENBLAS_NUM_THREADS='1'))
        def reader():
            for line in self.proc.stdout:
                self.messages.put(line)
            self.messages.put(None)
        self.thread = threading.Thread(target=reader, daemon=True)
        self.thread.start()
        assert self.read() == {'type': 'ready'}

    def read(self):
        try:
            line = self.messages.get(timeout=15)
        except queue.Empty:
            raise AssertionError('protocol response timeout')
        if line is None:
            self.stderr.seek(0)
            raise AssertionError('premature exit: ' + self.stderr.read().decode('utf-8', errors='replace'))
        assert line.endswith('\n')
        return json.loads(line)

    def request(self, sql=None, mode='exact', op='execute', ident=None):
        self.seq += 1
        ident = self.seq if ident is None else ident
        request = dict(id=ident, op=op)
        if sql is not None:
            request.update(sql=sql, search_mode=mode)
        assert self.messages.empty(), 'unsolicited stdout'
        self.proc.stdin.write(json.dumps(request, ensure_ascii=True) + '\n')
        self.proc.stdin.flush()
        response = self.read()
        assert type(response['id']) is type(ident) and response['id'] == ident
        assert type(response['ok']) is bool
        return response

    def sql(self, sql, mode='exact'):
        response = self.request(sql, mode)
        assert response['ok'], response
        return response

    def close(self):
        if self.proc.poll() is None:
            response = self.request(op='shutdown')
            assert response['ok']
            self.proc.stdin.close()
            self.proc.wait(timeout=15)
            self.thread.join(timeout=5)
            assert self.proc.returncode == 0
            assert self.messages.get(timeout=5) is None, 'extra stdout after shutdown'
        self.proc.stdout.close()
        if not self.proc.stdin.closed:
            self.proc.stdin.close()
        self.stderr.close()

    def crash(self):
        self.proc.kill()
        self.proc.wait(timeout=15)
        self.thread.join(timeout=5)
        self.proc.stdin.close()
        self.proc.stdout.close()
        self.stderr.close()

    def restart(self):
        self.close()
        self.start()


class PublicCases(unittest.TestCase):
    def test_all_public_cases(self):
        manifest = json.loads((ROOT / 'mysqltest/manifest.json').read_text(encoding='utf-8'))
        config = json.loads((ROOT / 'protocol_check/config.json').read_text(encoding='utf-8'))
        passed, sql_count = 0, 0
        for case in manifest['cases']:
            with self.subTest(case=case['id']), tempfile.TemporaryDirectory() as folder:
                client = Client(folder)
                try:
                    expected = json.loads((ROOT / 'mysqltest' / case['result']).read_text(encoding='utf-8'))
                    lines = (ROOT / 'mysqltest' / case['test']).read_text(encoding='utf-8').splitlines()
                    index, mode = 0, 'exact'
                    for line in lines:
                        line = line.strip()
                        if not line or line.startswith('#'):
                            continue
                        if line == '--restart':
                            client.restart()
                        elif line.startswith('--mode '):
                            mode = line.split()[1]
                        else:
                            assert_response(client.request(line, mode), expected[index], config)
                            index += 1
                    self.assertEqual(index, len(expected))
                    sql_count += index
                    passed += 1
                finally:
                    client.close()
        print(f'PUBLIC: {passed}/{len(manifest["cases"])} cases, {sql_count} SQL assertions', flush=True)


class Adversarial(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.client = Client(self.temp.name)

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def create(self):
        self.client.sql('CREATE TABLE t (id BIGINT PRIMARY KEY, s VARCHAR(4), v VECTOR(2));')

    def test_unicode_precision_projection_and_quoting(self):
        self.create()
        self.client.sql("INSERT INTO t (v,s,id) VALUES ('[16777217,0]','😀汉𐐀x',9223372036854775807);")
        result = self.client.sql('SELECT * FROM t;')['rows']
        self.assertEqual(result, [[9223372036854775807, '😀汉𐐀x', [16777216.0, 0.0]]])
        for sql in ["INSERT INTO t VALUES (1,'😀汉𐐀xy','[0,0]');", "INSERT INTO t VALUES (9223372036854775808,'x','[0,0]');"]:
            self.assertFalse(self.client.request(sql)['ok'])
        self.client.sql("INSERT INTO t VALUES (-9223372036854775808,'a'';b','[0,0]');")
        self.assertEqual(self.client.sql('SELECT s FROM t ORDER BY id;')['rows'][0], ["a';b"])

    def test_invalid_vectors_leave_no_rows(self):
        self.create()
        for literal in ["'[NaN,0]'", "'[Infinity,0]'", "'[1e100,0]'", "'[true,0]'", "'[1]'", "'[[1],2]'", "'[\"a\",0]'", '[1e309,0]']:
            with self.subTest(literal=literal):
                self.assertFalse(self.client.request(f"INSERT INTO t VALUES (1,'a',{literal});")['ok'])
        self.assertEqual(self.client.sql('SELECT id FROM t;')['rows'], [])

    def test_transactions_statements_and_index_visibility(self):
        self.create()
        self.client.sql("INSERT INTO t VALUES (1,'a','[0,0]'),(2,'b','[1,0]');")
        self.client.sql('CREATE VECTOR INDEX ix ON t(v);')
        self.client.sql('BEGIN;')
        self.client.sql("UPDATE t SET v='[10,0]' WHERE id=1;")
        self.assertFalse(self.client.request("INSERT INTO t VALUES (3,'c','[0,0]'),(2,'x','[2,0]');")['ok'])
        query = "SELECT id,L2_DISTANCE(v,'[0,0]') AS d FROM t ORDER BY d LIMIT 10;"
        self.assertEqual(self.client.sql(query, 'ann')['rows'], [[2, 1.0], [1, 10.0]])
        self.client.sql('DELETE FROM t WHERE id=2;')
        self.client.sql("INSERT INTO t VALUES (2,'b','[2,0]');")
        self.client.sql('ROLLBACK;')
        self.assertEqual(self.client.sql(query)['rows'], [[1, 0.0], [2, 1.0]])
        self.client.sql('BEGIN;')
        self.client.sql("UPDATE t SET s='z' WHERE id=1;")
        self.assertFalse(self.client.request("UPDATE t SET s='toolong' WHERE id=2;")['ok'])
        self.client.sql('COMMIT;')
        self.client.restart()
        self.assertEqual(self.client.sql('SELECT id,s FROM t ORDER BY id;')['rows'], [[1, 'z'], [2, 'b']])

    def test_ordering_filter_and_ties(self):
        self.create()
        self.client.sql("INSERT INTO t VALUES (9,'😀','[1,0]'),(3,'汉','[-1,0]'),(5,'汉','[0,1]'),(1,'a','[0,0]');")
        self.assertEqual(self.client.sql('SELECT id FROM t ORDER BY s DESC;')['rows'], [[9], [3], [5], [1]])
        query = "SELECT id,L2_DISTANCE(v,'[0,0]') AS d FROM t WHERE (s >= '汉' AND id < 9) ORDER BY d LIMIT 1;"
        self.assertEqual(self.client.sql(query)['rows'], [[3, 1.0]])
        self.assertEqual(self.client.sql('SELECT id FROM t ORDER BY s ASC,id DESC LIMIT 3;')['rows'], [[1], [5], [3]])
        self.assertEqual(self.client.sql('SELECT id FROM t LIMIT 0;')['rows'], [])

    def test_reject_unsupported_and_continue(self):
        self.create()
        for sql in ['SELECT id FROM t WHERE id=1 OR id=2;', 'SELECT COUNT(*) FROM t;', 'SELECT id FROM t; DELETE FROM t;', 'UPDATE t SET id=2;', 'SELECT id FROM t LIMIT 1 OFFSET 1;', 'ALTER TABLE t ADD x BIGINT;', 'SELECT id FROM t WHERE id != 1;']:
            self.assertFalse(self.client.request(sql)['ok'], sql)
        self.assertEqual(self.client.sql('SELECT id FROM t;')['rows'], [])

    def test_id_types_and_protocol_errors(self):
        for ident in ['42', 1.25, True, {'nested': [1, 2]}]:
            response = self.client.request(op='unknown', ident=ident)
            self.assertFalse(response['ok'])
        self.create()
        self.assertFalse(self.client.request('SELECT id FROM t;', mode='other')['ok'])
        self.assertEqual(self.client.sql('SELECT id FROM t;')['rows'], [])

    def test_kill_preserves_commit_discards_pending(self):
        self.create()
        self.client.sql("INSERT INTO t VALUES (1,'a','[0,0]');")
        self.client.sql('BEGIN;')
        self.client.sql("UPDATE t SET v='[3,4]' WHERE id=1;")
        self.client.sql("INSERT INTO t VALUES (2,'b','[0,0]');")
        self.client.crash()
        self.client.start()
        self.assertEqual(self.client.sql('SELECT * FROM t;')['rows'], [[1, 'a', [0.0, 0.0]]])
        self.client.sql('BEGIN;')
        self.client.sql("UPDATE t SET v='[3,4]' WHERE id=1;")
        self.client.sql('COMMIT;')
        self.client.crash()
        self.client.start()
        self.assertEqual(self.client.sql("SELECT L2_DISTANCE(v,'[0,0]') AS d FROM t;")['rows'], [[5.0]])

    def test_randomized_differential(self):
        self.create()
        rng, committed, current, active = random.Random(700132), {}, {}, False
        for step in range(350):
            action = rng.choice(['insert', 'delete', 'update', 'begin', 'commit', 'rollback', 'query'])
            pk = rng.randrange(-10, 35)
            if action == 'begin':
                response = self.client.request('BEGIN;')
                self.assertEqual(response['ok'], not active)
                if not active:
                    current, active = dict(committed), True
            elif action in ('commit', 'rollback'):
                self.client.sql(action.upper() + ';')
                if active:
                    if action == 'commit':
                        committed = dict(current)
                    current, active = dict(committed), False
            elif action == 'insert':
                value = (rng.choice(['a', '汉', '😀']), (rng.randrange(-5, 6), rng.randrange(-5, 6)))
                sql = f"INSERT INTO t VALUES ({pk},'{value[0]}','{list(value[1])}');"
                response = self.client.request(sql)
                self.assertEqual(response['ok'], pk not in current)
                if response['ok']:
                    current[pk] = value
            elif action == 'delete':
                response = self.client.sql(f'DELETE FROM t WHERE id={pk};')
                self.assertEqual(response['affected_rows'], int(pk in current))
                current.pop(pk, None)
            elif action == 'update':
                vector = (rng.randrange(-5, 6), rng.randrange(-5, 6))
                response = self.client.sql(f"UPDATE t SET v='{list(vector)}' WHERE id={pk};")
                self.assertEqual(response['affected_rows'], int(pk in current))
                if pk in current:
                    current[pk] = (current[pk][0], vector)
            if not active:
                committed = dict(current)
            if step % 7 == 0:
                q = (rng.randrange(-3, 4), rng.randrange(-3, 4))
                wanted = sorted([(key, math.dist(value[1], q)) for key, value in current.items() if value[0] >= '汉'], key=lambda r: (r[1], r[0]))[:6]
                actual = self.client.sql(f"SELECT id,L2_DISTANCE(v,'{list(q)}') AS d FROM t WHERE s >= '汉' ORDER BY d LIMIT 6;")['rows']
                self.assertEqual([r[0] for r in actual], [r[0] for r in wanted])
                for got, expected in zip(actual, wanted):
                    self.assertAlmostEqual(got[1], expected[1], places=12)
        print('DIFFERENTIAL: 350 seeded operations and filtered TopK cross-checks passed', flush=True)


class RecoveryFrames(unittest.TestCase):
    def test_indexed_multiblock_topk_matches_independent_scan(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(folder)
            rng = random.Random(192113)
            rows = []
            db.execute('CREATE TABLE t (id BIGINT PRIMARY KEY, group_id BIGINT, v VECTOR(8));')
            for batch in range(8):
                values = []
                for i in range(batch*100, batch*100+100):
                    vector = [struct.unpack('<f', struct.pack('<f', rng.uniform(-12, 12)))[0] for _ in range(8)]
                    rows.append((i, i % 4, vector))
                    values.append(f"({i},{i%4},'{json.dumps(vector)}')")
                db.execute('INSERT INTO t VALUES ' + ','.join(values) + ';')
            db.execute('CREATE VECTOR INDEX ix ON t(v);')
            for iteration in range(20):
                q = [struct.unpack('<f', struct.pack('<f', rng.uniform(-12, 12)))[0] for _ in range(8)]
                group = iteration % 4
                wanted = sorted([(pk, math.dist(vector, q)) for pk, g, vector in rows if g == group], key=lambda row: (row[1], row[0]))[:10]
                got = list(db.execute(f"SELECT id,L2_DISTANCE(v,'{json.dumps(q)}') AS d FROM t WHERE group_id={group} ORDER BY d LIMIT 10;", 'ann')['rows'])
                self.assertEqual([r[0] for r in got], [r[0] for r in wanted])
                for actual, expected in zip(got, wanted):
                    self.assertAlmostEqual(actual[1], expected[1], places=11)
            db.close()

    def test_external_merge_sort_and_cache_budget_fallback(self):
        import minivectordb.engine as engine
        old_sort, old_cache = engine.SORT_BUDGET, engine.CACHE_BUDGET
        engine.SORT_BUDGET, engine.CACHE_BUDGET = 512, 64
        try:
            with tempfile.TemporaryDirectory() as folder:
                db = Database(folder)
                db.execute('CREATE TABLE t (id BIGINT PRIMARY KEY, n BIGINT, v VECTOR(2));')
                db.execute('INSERT INTO t VALUES ' + ','.join(f"({i},{i%7},'[{i%5},0]')" for i in range(190)) + ';')
                rows = list(db.execute('SELECT id,n FROM t ORDER BY n DESC;')['rows'])
                self.assertEqual(rows, [[i, i%7] for i in sorted(range(190), key=lambda i: (-(i%7), i))])
                self.assertFalse(list(db.store.scratch.glob('sort-*')))
                rows = list(db.execute("SELECT id,L2_DISTANCE(v,'[0,0]') AS d FROM t ORDER BY d LIMIT 3;")['rows'])
                self.assertEqual(rows, [[0, 0.0], [5, 0.0], [10, 0.0]])
                db.execute('CREATE VECTOR INDEX ix ON t(v);')
                db.execute('BEGIN;')
                db.execute("UPDATE t SET v='[99,0]' WHERE id=0;")
                db.execute('DELETE FROM t WHERE id=5;')
                rows = list(db.execute("SELECT id,L2_DISTANCE(v,'[0,0]') AS d FROM t ORDER BY d LIMIT 3;")['rows'])
                self.assertEqual(rows, [[10, 0.0], [15, 0.0], [20, 0.0]])
                db.execute('COMMIT;')
                db.close()
                db = Database(folder)
                rows = list(db.execute("SELECT id,L2_DISTANCE(v,'[0,0]') AS d FROM t WHERE id<20 ORDER BY d LIMIT 2;")['rows'])
                self.assertEqual(rows, [[10, 0.0], [15, 0.0]])
                db.close()
        finally:
            engine.SORT_BUDGET, engine.CACHE_BUDGET = old_sort, old_cache

    def test_every_torn_commit_prefix_is_atomic(self):
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as target:
            db = Database(source)
            db.execute('CREATE TABLE t (id BIGINT PRIMARY KEY, n BIGINT);')
            db.execute('INSERT INTO t VALUES (1,10);')
            before = (Path(source) / 'commit.log').stat().st_size
            db.execute('BEGIN;')
            db.execute('UPDATE t SET n=99 WHERE id=1;')
            db.execute('INSERT INTO t VALUES (2,20),(3,30);')
            db.execute('COMMIT;')
            db.close()
            raw = (Path(source) / 'commit.log').read_bytes()
            for prefix in range(before, len(raw) + 1):
                with self.subTest(prefix=prefix):
                    (Path(target) / 'commit.log').write_bytes(raw[:prefix])
                    db = Database(target)
                    rows = list(db.execute('SELECT id,n FROM t ORDER BY id;')['rows'])
                    self.assertEqual(rows, [[1, 99], [2, 20], [3, 30]] if prefix == len(raw) else [[1, 10]])
                    db.close()
            print(f'RECOVERY: {len(raw)-before+1} commit byte prefixes recovered atomically', flush=True)

    def test_disk_index_growth_signed_keys_and_duplicate_atomicity(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(folder)
            db.execute('CREATE TABLE t (id BIGINT PRIMARY KEY, n BIGINT);')
            for batch in range(24):
                values = ','.join(f'({i-1200},{i})' for i in range(batch*100, batch*100+100))
                db.execute('INSERT INTO t VALUES ' + values + ';')
            self.assertEqual(len(list(db.execute('SELECT id FROM t;')['rows'])), 2400)
            with self.assertRaises(DBError):
                db.execute('INSERT INTO t VALUES (3000,1),(1,99);')
            self.assertEqual(list(db.execute('SELECT id FROM t WHERE id=3000;')['rows']), [])
            db.close()
            db = Database(folder)
            self.assertEqual(list(db.execute('SELECT n FROM t WHERE id=-1200;')['rows']), [[0]])
            self.assertEqual(list(db.execute('SELECT n FROM t WHERE id=1199;')['rows']), [[2399]])
            db.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
