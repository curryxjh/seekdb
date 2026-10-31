#!/usr/bin/env python3
"""Check concurrent index builds and DML on a dedicated temporary fixture."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client', required=True)
    parser.add_argument('--port', type=int, default=2883)
    args = parser.parse_args()
    command = [args.client, '-h127.0.0.1', '-P' + str(args.port), '-uroot',
               '--batch', '--raw', '--skip-column-names', '--default-character-set=utf8mb4']

    def sql(statement):
        result = subprocess.run(command, input=(
            'SET NAMES utf8mb4; SET ob_query_timeout=1000000000; '
            'USE fts_ddl_concurrency; ' + statement).encode(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode:
            raise RuntimeError(result.stderr.decode())
        return result.stdout.decode().splitlines()

    subprocess.run(command, input=b'CREATE DATABASE IF NOT EXISTS fts_ddl_concurrency;', check=True)
    english = ['database audit', 'performance reliability', 'security archive', 'tokenizer pipeline']
    chinese = ['数据库 全文索引', '性能 优化', '检索 稳定', '数据安全']
    documents = {i: (english[i % 4], chinese[i % 4]) for i in range(1, 2001)}
    for table in ['english_docs', 'chinese_docs']:
        sql('DROP TABLE IF EXISTS %s; CREATE TABLE %s('
            'id INT PRIMARY KEY,en VARCHAR(200),cn VARCHAR(200));' % (table, table))
        for start in range(1, 2001, 200):
            values = ','.join("(%d,'%s','%s')" % (i, *documents[i])
                              for i in range(start, start + 200))
            sql('INSERT INTO %s VALUES %s;' % (table, values))

    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs = [pool.submit(sql, 'ALTER TABLE english_docs ADD FULLTEXT INDEX fti(en) WITH PARSER beng;'),
                pool.submit(sql, 'ALTER TABLE chinese_docs ADD FULLTEXT INDEX fti(cn) WITH PARSER ik;'),
                pool.submit(sql, 'CREATE INDEX ordinary_cn ON english_docs(cn);')]
        # These changes must be reflected whether they precede or follow the
        # build snapshot. Check the final index against the source documents.
        for table in ['english_docs', 'chinese_docs']:
            sql("UPDATE %s SET en='database security',cn='数据库 检索' WHERE MOD(id,13)=0;"
                "DELETE FROM %s WHERE MOD(id,17)=0;" % (table, table))
        for job in jobs:
            job.result()
    documents = {i: (('database security', '数据库 检索') if i % 13 == 0 else value)
                 for i, value in documents.items() if i % 17 != 0}
    for table, column, index, words in [
            ('english_docs', 'en', 0, ['database', 'security', 'database security', 'zzznomatch']),
            ('chinese_docs', 'cn', 1, ['数据库', '检索', '数据库 检索', 'zzznomatch'])]:
        actual_ids = [int(i) for i in sql('SELECT id FROM %s ORDER BY id;' % table)]
        assert actual_ids == sorted(documents), table
        for text in words:
            expected = sorted(i for i, value in documents.items()
                              if any(term in value[index].split() for term in text.split()))
            actual = [int(i) for i in sql("SELECT id FROM %s WHERE MATCH(%s) AGAINST('%s') "
                                         "ORDER BY id;" % (table, column, text))]
            assert actual == expected, (table, text, len(actual), len(expected))
    sql('DROP TABLE english_docs,chinese_docs;')
    print('PASS: concurrent IK/BENG/B-tree builds and DML preserve every matching ID')


if __name__ == '__main__':
    main()
