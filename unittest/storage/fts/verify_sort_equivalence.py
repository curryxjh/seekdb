#!/usr/bin/env python3
"""Compare complete shared-sort results across original and candidate servers."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client', required=True)
    parser.add_argument('--port', default='2883')
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--record', action='store_true')
    args = parser.parse_args()
    statements = [
        'SET NAMES utf8mb4;',
        'CREATE DATABASE IF NOT EXISTS fts_sort_regression;',
        'USE fts_sort_regression;',
        'DROP TABLE IF EXISTS sort_keys;',
        'CREATE TABLE sort_keys(id INT PRIMARY KEY, k VARCHAR(300)) CHARSET=utf8mb4;',
        "INSERT INTO sort_keys SELECT id, ELT(1 + MOD(id, 16), "
        "'e','E','é','É','e ','e  ','Ａ','a','中','文','😀','𠀀','',"
        "'ß','ss','Z') FROM fts_large_bench.docs;",
    ]
    for collation in ['utf8mb4_general_ci', 'utf8mb4_unicode_ci', 'utf8mb4_bin']:
        for direction in ['ASC', 'DESC']:
            for suffix in ['', ' LIMIT 300 OFFSET 4000']:
                statements.append(
                    'SELECT id,k FROM sort_keys ORDER BY k COLLATE %s %s,id DESC%s;'
                    % (collation, direction, suffix))
    # NULLs, long keys, high cardinality and nonstring first keys use fallbacks.
    statements += [
        'UPDATE sort_keys SET k=NULL WHERE MOD(id,101)=0;',
        'SELECT id,k FROM sort_keys ORDER BY k,id;',
        'SELECT id,k FROM sort_keys ORDER BY k DESC,id;',
        "UPDATE sort_keys SET k=REPEAT('z',280) WHERE MOD(id,47)=0;",
        'SELECT id,k FROM sort_keys ORDER BY k,id;',
        'UPDATE sort_keys SET k=CAST(id AS CHAR);',
        'SELECT id,k FROM sort_keys ORDER BY k,id;',
        'SELECT id,k FROM sort_keys ORDER BY MOD(id,17),k,id;',
        'ALTER TABLE sort_keys ADD b VARBINARY(300);',
        "UPDATE sort_keys SET b=UNHEX(LPAD(HEX(id),32,'0'));",
        'SELECT id,HEX(b) FROM sort_keys ORDER BY b,id;',
        'SELECT id,HEX(b) FROM sort_keys ORDER BY b DESC,id;',
        # Repeated binary leading keys with text and numeric suffixes exercise
        # dictionary ranks in both directions, including collation ties.
        "UPDATE sort_keys SET b=UNHEX(LPAD(HEX(MOD(id,97)),32,'0'));",
        'SELECT id,HEX(b),k FROM sort_keys ORDER BY b,k COLLATE utf8mb4_unicode_ci,id;',
        'SELECT id,HEX(b),k FROM sort_keys ORDER BY b DESC,k COLLATE utf8mb4_unicode_ci DESC,id DESC;',
        "UPDATE sort_keys SET b=CONCAT(CHAR(MOD(id,256)),REPEAT(CHAR(0),MOD(id,25)),CHAR(255));",
        'UPDATE sort_keys SET b=NULL WHERE MOD(id,101)=0;',
        'SELECT id,HEX(b) FROM sort_keys ORDER BY b,id;',
        'SELECT id,HEX(b) FROM sort_keys ORDER BY b DESC,id DESC;',
        # The inner order feeds monotonic leading groups to the shared sort.
        'SELECT id,k FROM (SELECT /*+ NO_MERGE */ id,k,MOD(id,19) g '
        'FROM sort_keys ORDER BY g) t ORDER BY g,k DESC,id;',
        'DROP DATABASE fts_sort_regression;',
    ]
    sql = '\n'.join(statements)
    run = subprocess.run([args.client, '-h127.0.0.1', '-P' + args.port, '-uroot',
                          '--batch', '--raw', '--skip-column-names',
                          '--default-character-set=utf8mb4'], input=sql.encode(),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    result = dict(sql_sha256=hashlib.sha256(sql.encode()).hexdigest(),
                  results=run.stdout.decode())
    if args.record:
        if args.reference.exists():
            raise SystemExit('Reference already exists')
        args.reference.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    else:
        expected = json.loads(args.reference.read_text(encoding='utf-8'))
        if result != expected:
            args.reference.with_suffix('.candidate.json').write_text(
                json.dumps(result, ensure_ascii=False), encoding='utf-8')
            raise SystemExit('Sort result mismatch')
    print('PASS: shared sort result rows:', len(run.stdout.splitlines()))
    print('sha256:', hashlib.sha256(run.stdout).hexdigest())


if __name__ == '__main__':
    main()
