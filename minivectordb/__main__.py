"""Foreground JSONL server. Stdout is reserved exclusively for the protocol."""
import argparse
import json
import os
import signal
import sys
import traceback

from .engine import Database, strict_json
from .sql import DBError


def emit(response):
    if 'rows' not in response:
        sys.stdout.write(json.dumps(response, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n')
    else:
        rows = response['rows']
        header = {k: v for k, v in response.items() if k != 'rows'}
        sys.stdout.write(json.dumps(header, ensure_ascii=False, allow_nan=False, separators=(',', ':'))[:-1] + ',"rows":[')
        first = True
        for row in rows:
            if not first:
                sys.stdout.write(',')
            sys.stdout.write(json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(',', ':')))
            first = False
        sys.stdout.write(']}\n')
    sys.stdout.flush()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', default=os.environ.get('VECTORDB_DATA_DIR', '/data'))
    args = parser.parse_args()
    if hasattr(sys.stdin, 'reconfigure'):
        sys.stdin.reconfigure(encoding='utf-8')
        sys.stdout.reconfigure(encoding='utf-8', newline='\n')
    database = Database(args.data_dir)
    def stop(signum, frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        emit({'type': 'ready'})
        for line in sys.stdin:
            ident = None
            try:
                request = strict_json(line)
                if not isinstance(request, dict) or 'id' not in request:
                    raise DBError('invalid_request', 'request must be an object with id')
                ident = request['id']
                if request.get('op') == 'shutdown':
                    database.close()
                    database = None
                    emit({'id': ident, 'ok': True})
                    return
                if request.get('op') != 'execute':
                    raise DBError('unsupported_op', 'unsupported operation')
                result = database.execute(request.get('sql'), request.get('search_mode', 'exact'))
                response = {'id': ident, 'ok': True, **result}
            except DBError as exc:
                response = {'id': ident, 'ok': False, 'error': {'code': exc.code, 'message': str(exc)}}
            except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
                response = {'id': ident, 'ok': False, 'error': {'code': 'invalid_request', 'message': str(exc)}}
            # Storage failures are fatal: never append another commit after a torn
            # frame, nor claim that an ambiguously persisted transaction failed.
            emit(response)
    finally:
        if database is not None:
            database.close()


if __name__ == '__main__':
    try:
        main()
    except BrokenPipeError:
        sys.exit(1)
    except Exception:
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
