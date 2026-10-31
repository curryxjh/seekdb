#!/usr/bin/env python3
"""Compare aggregate MATCH predicates, including expressions that need scores."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client', required=True)
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--record', action='store_true')
    args = parser.parse_args()
    statements = ['SET NAMES utf8mb4; USE fts_large_bench;']
    for text in ['database', 'database audit', 'database performance audit',
                 'zzzunmatchedterm', 'database zzzunmatchedterm', '']:
        match = "MATCH(title_en,content_en) AGAINST('%s')" % text
        for projection in ['COUNT(*)', 'COUNT(id),SUM(id),MIN(id),MAX(id),BIT_XOR(id)',
                           'COUNT(DISTINCT id)', 'SUM(%s)' % match,
                           'COUNT(%s)' % match, 'MIN(%s),MAX(%s)' % (match, match)]:
            for predicate in [match, match + ' AND MOD(id,3)=0',
                              match + ' > 0', match + ' > 0.01',
                              match + ' > 1.0', '(' + match + ') IS TRUE']:
                statements.append('SELECT %s FROM docs WHERE %s;' % (projection, predicate))
        statements += [
            'SELECT MOD(id,3),COUNT(*) FROM docs WHERE %s GROUP BY MOD(id,3) ORDER BY 1;' % match,
            'SELECT COUNT(*) FROM docs WHERE %s HAVING MAX(%s)>0;' % (match, match),
        ]
    for boolean in ['+database -audit', '+database +performance']:
        statements.append("SELECT COUNT(*),SUM(id) FROM docs WHERE "
                          "MATCH(title_en,content_en) AGAINST('%s' IN BOOLEAN MODE);" % boolean)
    for text in ['database audit', 'zzzunmatchedterm', '']:
        match = "MATCH(title_en,content_en) AGAINST('%s')" % text
        for limit in [0, 1, 20, 30000]:
            for offset in [0, 3, 20001]:
                for output in ['id', '1', 'id,title_en', match + ' AS score']:
                    statements.append('SELECT COUNT(*) FROM (SELECT %s FROM docs '
                                      'WHERE %s LIMIT %d OFFSET %d) v;' %
                                      (output, match, limit, offset))
        for output in ['id', 'id,title_en']:
            statements.append('SELECT COUNT(id),SUM(id) FROM (SELECT %s FROM docs '
                              'WHERE %s ORDER BY id LIMIT 20) v;' % (output, match))
        statements.append('SELECT COUNT(*) FROM (SELECT id FROM docs WHERE %s > 1 '
                          'ORDER BY id DESC LIMIT 20) v;' % match)
    statements += [
        'DROP TABLE IF EXISTS cardinality_projection;',
        'CREATE TABLE cardinality_projection(id INT PRIMARY KEY, val INT);',
        'INSERT INTO cardinality_projection VALUES (1,NULL),(2,10),(3,NULL),(4,20);',
        'SELECT COUNT(*) FROM (SELECT val FROM cardinality_projection LIMIT 3) v;',
        'SELECT COUNT(val) FROM (SELECT val FROM cardinality_projection ORDER BY id LIMIT 3) v;',
        'SELECT COUNT(*) FROM (SELECT DISTINCT val FROM cardinality_projection LIMIT 3) v;',
        'SELECT COUNT(*) FROM (SELECT val,COUNT(*) FROM cardinality_projection GROUP BY val LIMIT 2) v;',
        'SELECT COUNT(*) FROM (SELECT val FROM cardinality_projection WHERE val IS NULL LIMIT 3) v;',
        'SELECT COUNT(*) FROM (SELECT id FROM cardinality_projection ORDER BY val LIMIT 2) v;',
        'DROP TABLE cardinality_projection;',
    ]
    statements += [
        "PREPARE aggregate_match FROM 'SELECT COUNT(*) FROM docs WHERE "
        "MATCH(title_en,content_en) AGAINST(?) > ?';",
        "SET @q='database audit'; SET @threshold=0; EXECUTE aggregate_match USING @q,@threshold;",
        'SET @threshold=1; EXECUTE aggregate_match USING @q,@threshold;',
        'DEALLOCATE PREPARE aggregate_match;',
    ]
    sql = '\n'.join(statements)
    run = subprocess.run([args.client, '-h127.0.0.1', '-P2883', '-uroot',
                          '--batch', '--raw', '--skip-column-names',
                          '--default-character-set=utf8mb4'], input=sql.encode(),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if run.returncode:
        raise SystemExit(run.stderr.decode())
    actual = dict(sql_sha256=hashlib.sha256(sql.encode()).hexdigest(),
                  results=run.stdout.decode())
    if args.record:
        if args.reference.exists():
            raise SystemExit('Reference already exists')
        args.reference.write_text(json.dumps(actual), encoding='utf-8')
    elif actual != json.loads(args.reference.read_text(encoding='utf-8')):
        args.reference.with_suffix('.candidate.json').write_text(json.dumps(actual), encoding='utf-8')
        raise SystemExit('Aggregate MATCH result mismatch')
    print('PASS: aggregate result rows:', len(run.stdout.splitlines()))
    print('sha256:', hashlib.sha256(run.stdout).hexdigest())


if __name__ == '__main__':
    main()
