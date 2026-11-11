"""Bounded-memory storage, container kills and interrupted-recovery smoke checks."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'protocol_check'))
from _transport import Session


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', default='minivectordb:local')
    parser.add_argument('--rows', type=int, default=6000)
    parser.add_argument('--width', type=int, default=32768)
    parser.add_argument('--memory', default='128m')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    config = json.loads((ROOT / 'protocol_check/config.json').read_text())
    config.update(memory=args.memory, case_timeout=1200, request_timeout=120, startup_timeout=120)
    session = Session(config, args.output/'stderr.log', image=args.image)
    # Freeze the tested image across restarts even if the tag is rebuilt elsewhere.
    session.image = session.docker('image', 'inspect', '--format', '{{.Id}}', args.image).stdout.decode().strip()
    started = time.perf_counter()
    operations = 0
    def sql(text):
        nonlocal operations
        result = session.request(text)
        assert result['ok'], result
        operations += 1
        return result
    try:
        session.start()
        sql(f'CREATE TABLE t (id BIGINT PRIMARY KEY, text VARCHAR({args.width}), v VECTOR(2));')
        text = 'x' * args.width
        for batch in range(0, args.rows, 50):
            values = ','.join(f"({i},'{text}','[{i},0]')" for i in range(batch, min(batch+50, args.rows)))
            sql('INSERT INTO t VALUES ' + values + ';')
        write_seconds = time.perf_counter() - started
        # count all IDs without allocating all document strings in the client.
        ids = sql('SELECT id FROM t;')['rows']
        assert len(ids) == args.rows and len({r[0] for r in ids}) == args.rows
        assert sql(f'SELECT id FROM t WHERE id={args.rows-1};')['rows'] == [[args.rows-1]]
        assert sql('SELECT id FROM t WHERE id>=10 AND id<13 ORDER BY id;')['rows'] == [[10],[11],[12]]
        sql('CREATE VECTOR INDEX ix ON t(v);')
        query = f"SELECT id,L2_DISTANCE(v,'[{args.rows},0]') AS d FROM t ORDER BY d LIMIT 3;"
        assert sql(query)['rows'] == [[args.rows-1,1.0],[args.rows-2,2.0],[args.rows-3,3.0]]
        sql('BEGIN;')
        sql("UPDATE t SET text='pending' WHERE id=0;")
        sql('DELETE FROM t WHERE id=1;')
        session.crash()
        session.start()
        assert sql('SELECT text FROM t WHERE id=0;')['rows'] == [[text]]
        assert sql('SELECT id FROM t WHERE id=1;')['rows'] == [[1]]
        sql('BEGIN;')
        sql("UPDATE t SET text='committed' WHERE id=0;")
        sql('COMMIT;')
        session.crash()
        # Trigger recovery, kill before waiting for ready, and repeat.
        interrupted = 0
        for delay in (0.05, 0.12, 0.25):
            session.start(ready=False)
            running = False
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                state = session.docker('inspect', '--format', '{{.State.Running}}', session.name, check=False)
                if state.stdout.strip() == b'true':
                    running = True
                    break
            assert running, 'container did not start'
            time.sleep(delay)
            session.crash()
            interrupted += 1
        session.start()
        assert sql('SELECT text FROM t WHERE id=0;')['rows'] == [['committed']]
        assert len(sql('SELECT id FROM t;')['rows']) == args.rows
        assert sql(query)['rows'] == [[args.rows-1,1.0],[args.rows-2,2.0],[args.rows-3,3.0]]
        measurement = session.docker('exec', session.name, 'python', '-c',
            "import json,pathlib; p=pathlib.Path('/sys/fs/cgroup'); print(json.dumps({k:(p/k).read_text().strip() for k in ['memory.peak','memory.max','memory.events','io.stat']})); print(pathlib.Path('/data/commit.log').stat().st_size)")
        lines = measurement.stdout.decode().splitlines()
        resources, data_bytes = json.loads(lines[0]), int(lines[1])
        # Read-only root and offline networking are set by the unmodified Session.
        report = dict(passed=True, image_id=session.image, rows=args.rows, width=args.width, logical_text_bytes=args.rows*args.width,
            log_bytes=data_bytes, memory_limit=args.memory, write_seconds=write_seconds,
            total_seconds=time.perf_counter()-started, sql_operations=operations,
            forced_recovery_restarts=interrupted, resources=resources,
            checks=['all IDs', 'primary-key reads', 'exact filtered visibility', 'bounded TopK fallback',
                'uncommitted update/delete rollback after SIGKILL', 'committed update after SIGKILL', 'repeated startup interruption'],
            limitation='Local Docker smoke check; startup kills are deliberately timed and do not prove the exact instruction at which recovery was interrupted. Final cgroup metrics describe the final restart, not all earlier container lifetimes. Not official benchmark metrics.')
        session.shutdown()
        (args.output/'result.json').write_text(json.dumps(report, indent=2)+'\n')
        print(json.dumps(report, indent=2))
    finally:
        session.close()


if __name__ == '__main__':
    main()
