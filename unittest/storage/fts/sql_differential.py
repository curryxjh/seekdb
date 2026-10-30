#!/usr/bin/env python3
"""Capture/compare complete FTS results against a separately built baseline.

Requires PyMySQL and an isolated Linux seekdb instance. The runner creates and
removes its own uniquely named database; it does not touch the official benchmark.
Passwords are read from FTS_TEST_PASSWORD, never from command-line arguments.
"""

import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import random
import uuid

import pymysql


def connect(args):
    return pymysql.connect(host=args.host, port=args.port, user=args.user,
                           password=os.environ.get("FTS_TEST_PASSWORD", ""),
                           charset="utf8mb4", autocommit=True)


def corpus():
    texts = [None, "", " ", "\t\r\n", "the THE and AND", "a ab abc abcd",
             "Database DATABASE database", "中文全文索引，数据库检索。",
             "中华人民共和国南京市长江大桥", "中国人民银行研究生命起源",
             "OceanBase数据库 database性能", "一二三四五十百千万123.45公斤",
             "ＡＢＣ１２３ full-width", "éÉßİ café Straße", "日本語かなカナ한국어",
             "😀🀄𠀀𠮷中文", "abc\x00DEF\x01ghi", "foo-bar foo_bar a@b.com",
             "a" * 2, "b" * 3, "c" * 84, "d" * 85,
             "中文 " * 1000, "repeat " * 1000]
    rng = random.Random(20261030)
    words = ["数据库", "全文索引", "北京", "南京", "研究", "生命", "database",
             "performance", "audit", "The", "ABC", "123", "😀", "café", "日本語"]
    for _ in range(100):
        texts.append(rng.choice([" ", "，", "\n", "-", ""]).join(
            rng.choices(words, k=rng.randrange(1, 45))))
    return texts


def token_results(args, texts):
    result = []
    with connect(args) as db, db.cursor() as cur:
        for parser in ("ik", "beng", "space", "ngram"):
            for text in texts:
                # Preserve complete arrays, term frequencies and output order.
                for options in (None, '[{"output":"all"}]'):
                    try:
                        if options is None:
                            cur.execute("SELECT TOKENIZE(%s, %s) AS tokens", (text, parser))
                        else:
                            cur.execute("SELECT TOKENIZE(%s, %s, %s) AS tokens", (text, parser, options))
                        value = cur.fetchone()[0]
                        result.append(json.loads(value) if value is not None else None)
                    except pymysql.MySQLError as error:
                        # NULL has binary charset on the original server; preserve
                        # its rejection as part of the observable contract.
                        if text is not None:
                            raise
                        result.append({"error": list(error.args)})
    return result


def filter_results(cur):
    """Exercise the native BOOL(score) path and expression-evaluation fallback."""
    def capture_query(sql, params):
        try:
            cur.execute(sql, params)
            return [list(row) for row in cur.fetchall()]
        except pymysql.MySQLError as error:
            # Some Boolean operators are not supported by this branch. Their
            # original rejection is part of the baseline contract as well.
            if error.args[0] not in (1149, 1235):
                raise
            return {"error": list(error.args)}

    result = []
    for mode in ("IN NATURAL LANGUAGE MODE", "IN BOOLEAN MODE"):
        for query in ("database", "database audit", "+database -audit", "~database audit", "missingnotfound"):
            match = "MATCH(title,body) AGAINST(%s " + mode + ")"
            for condition in (match, match + " > 0", match + " > 0.2",
                              match + " BETWEEN 0.1 AND 100", "NOT (" + match + ")"):
                result.append(capture_query("SELECT id," + match + " AS score FROM docs WHERE " +
                                            condition + " ORDER BY id", (query, query)))
                result.append(capture_query("SELECT COUNT(*) FROM docs WHERE " + condition, (query,)))
            result.append(capture_query("SELECT id," + match + " AS score FROM docs WHERE " + match +
                                        " ORDER BY score DESC,id LIMIT 3 OFFSET 2", (query, query)))
            # The same expression may also be projected above the scan. Make
            # sure bypassing the filter evaluator leaves no stale flags/data.
            result.append(capture_query("SELECT id, (" + match + ") IS TRUE FROM docs WHERE " +
                                        match + " ORDER BY id", (query, query)))
    return result


def capture(args):
    texts = corpus()
    output = {"format": 2, "corpus": texts, "tokens": token_results(args, texts)}
    # Independent connections exercise dictionary sharing and parser lifetimes.
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        parallel = list(pool.map(lambda _: token_results(args, texts[:24]), range(4)))
    if any(item != parallel[0] for item in parallel[1:]):
        raise AssertionError("concurrent TOKENIZE results differ")
    output["concurrent_tokens"] = parallel[0]
    name = "fts_diff_" + uuid.uuid4().hex
    with connect(args) as db, db.cursor() as cur:
        cur.execute("CREATE DATABASE " + name)
        try:
            cur.execute("USE " + name)
            cur.execute("SET ob_query_timeout=1000000000")
            output["queries"] = {}
            output["filters"] = {}
            for parser in ("ik", "beng"):
                cur.execute("CREATE TABLE docs (id BIGINT PRIMARY KEY, title TEXT, body TEXT)")
                rows = [(i, text, texts[(i + 7) % len(texts)])
                        for i, text in enumerate(texts)]
                cur.executemany("INSERT INTO docs VALUES (%s,%s,%s)", rows)
                cur.execute("CREATE FULLTEXT INDEX ft ON docs(title,body) WITH PARSER " + parser)
                stages = {}
                filter_stages = {}
                for stage in ("build", "dml", "rebuild"):
                    if stage == "dml":
                        cur.execute("UPDATE docs SET body='数据库 database audit' WHERE MOD(id,7)=0")
                        cur.execute("DELETE FROM docs WHERE MOD(id,11)=0")
                        cur.execute("INSERT INTO docs VALUES (999,'数据库','database audit')")
                    elif stage == "rebuild":
                        cur.execute("DROP INDEX ft ON docs")
                        cur.execute("CREATE FULLTEXT INDEX ft ON docs(title,body) WITH PARSER " + parser)
                    results = []
                    for query in ("数据库", "database", "全文索引 分词器", "OceanBase database",
                                  "南京 生命", "missingnotfound", "database database", ""):
                        for mode in ("IN NATURAL LANGUAGE MODE", "IN BOOLEAN MODE"):
                            match = "MATCH(title,body) AGAINST(%s " + mode + ")"
                            for limit in ("", " LIMIT 7"):
                                cur.execute("SELECT id," + match + " AS score FROM docs WHERE " +
                                            match + " ORDER BY score DESC,id" + limit, (query, query))
                                results.append([list(row) for row in cur.fetchall()])
                    stages[stage] = results
                    filter_stages[stage] = filter_results(cur)
                # Rebuilding can refresh corpus statistics and hence scores.
                # Compare the complete baseline for each stage separately.
                for before, after in zip(stages["dml"][::2], stages["rebuild"][::2]):
                    if sorted(row[0] for row in before) != sorted(row[0] for row in after):
                        raise AssertionError("DML and rebuilt index memberships differ")
                output["queries"][parser] = stages
                output["filters"][parser] = filter_stages
                cur.execute("DROP TABLE docs")
        finally:
            cur.execute("DROP DATABASE " + name)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--user", default="root")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reference", type=Path)
    args = parser.parse_args()
    result = capture(args)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    if args.reference:
        expected = json.loads(args.reference.read_text())
        if result != expected:
            differing = [key for key in result if result[key] != expected.get(key)]
            raise AssertionError("baseline mismatch in: " + ", ".join(differing))
    print("PASS: complete FTS results saved to", args.output)


if __name__ == "__main__":
    main()
