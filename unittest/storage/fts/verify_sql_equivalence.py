#!/usr/bin/env python3
"""Compare complete TOKENIZE/MATCH results from two binaries on one fixture.

Run --record on the original server, then restart the same dedicated instance
with the candidate and rerun without --record. This never changes bench data.
The optional benchmark database must already contain the official fixture.
"""
import argparse
import hashlib
import json
from pathlib import Path
import random
import subprocess


def literal(value):
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def queries(include_benchmark):
    rng = random.Random(20261030)
    alphabet = "数据库全文索引分词器性能中华人民共和国一百二十三个年月日" \
               "ABCXYZabcxyz0123456789ＡＢＣａｂｃ１２３あいうカタカナ한글" \
               "Ééßİı😀𠀀 \t\n.,-_@+#&!"
    documents = ["", "a", "the THE", "数据库 数据库 database DATABASE",
                 "\x01\x02\t\n", "a" * 84, "A" * 1024,
                 "中华人民共和国数据库全文索引优化。" * 256]
    for _ in range(200):
        length = rng.choice([1, 2, 7, 31, 64, 257, 1024])
        documents.append("".join(rng.choice(alphabet) for _ in range(length)))
    result = []
    # Default properties take a direct path; an explicit empty argument
    # object still exercises the validated JSON path for every parser.
    for parser in ["ik", "beng", "space", "ngram", "ngram2"]:
        for props in ['[{"output":"all"}]',
                      '[{"output":"all"},{"additional_args":[]}]']:
            result.append("SELECT %d AS case_id, TOKENIZE(%s,%s,%s);" %
                          (len(result), literal("数据库 HELLO hello ÉCOLE 😀 𠀀"),
                           literal(parser), literal(props)))
    for text in documents:
        for parser, props in [
            ("ik", '[{"output":"all"},{"additional_args":[{"ik_mode":"smart"}]}]'),
            ("ik", '[{"output":"all"},{"additional_args":[{"ik_mode":"max_word"}]}]'),
            ("beng", '[{"output":"all"}]'), ("space", '[{"output":"all"}]')]:
            result.append("SELECT %d AS case_id, TOKENIZE(%s,%s,%s);" %
                          (len(result), literal(text), literal(parser), literal(props)))
    if include_benchmark:
        for columns, text in [("title, content", "全文索引 分词器"),
                              ("title_en, content_en", "database performance audit"),
                              ("title, content", "OceanBase 稳定 database"),
                              ("content", "倒排索引 tokenizer")]:
            match = "MATCH(%s) AGAINST(%s)" % (columns, literal(text))
            # Capture every matching ID and score, with deterministic row order.
            result.append("SELECT id, %s AS score FROM fts_large_bench.docs "
                          "WHERE %s ORDER BY id;" % (match, match))
        # Exercise the short-query merge (one to three terms), the general
        # merge, Boolean collectors, score predicates, ties, and offsets.
        for columns, texts in [
            ("title_en, content_en", ["database", "database audit", "database performance audit",
                                      "database performance audit security reliability"]),
            ("title, content", ["数据库", "数据库 索引", "OceanBase 稳定 database",
                                "全文索引 分词器 数据库 检索 性能 优化"]),
        ]:
            for text in texts:
                match = "MATCH(%s) AGAINST(%s)" % (columns, literal(text))
                for predicate in [match, match + " > 0.01", match + " > 1.0"]:
                    result.append("SELECT id, %s AS score FROM fts_large_bench.docs "
                                  "WHERE %s ORDER BY id;" % (match, predicate))
                result.append("SELECT id, %s AS score FROM fts_large_bench.docs "
                              "WHERE %s ORDER BY score DESC, id LIMIT 23 OFFSET 7;" % (match, match))
        for text in ["+database -audit", "+database +performance", "database audit",
                     "+database +(performance audit)"]:
            match = "MATCH(title_en, content_en) AGAINST(%s IN BOOLEAN MODE)" % literal(text)
            result.append("SELECT id, %s AS score FROM fts_large_bench.docs "
                          "WHERE %s ORDER BY id;" % (match, match))
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--client", required=True)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=2883)
    p.add_argument("--user", default="root")
    p.add_argument("--reference", type=Path, required=True)
    p.add_argument("--record", action="store_true")
    p.add_argument("--benchmark-database", action="store_true")
    args = p.parse_args()
    sql = "SET NAMES utf8mb4;\n" + "\n".join(queries(args.benchmark_database))
    run = subprocess.run([args.client, "-h" + args.host, "-P" + str(args.port),
                          "-u" + args.user, "--batch", "--raw", "--skip-column-names",
                          "--default-character-set=utf8mb4"], input=sql.encode(),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if run.returncode:
        raise SystemExit(run.stderr.decode("utf-8", errors="replace"))
    actual = {"sql_sha256": hashlib.sha256(sql.encode()).hexdigest(),
              "results": run.stdout.decode("utf-8")}
    if args.record:
        if args.reference.exists():
            raise SystemExit("Reference exists; choose a new path to preserve the original evidence")
        args.reference.write_text(json.dumps(actual, ensure_ascii=False), encoding="utf-8")
        print("Recorded", args.reference)
    else:
        expected = json.loads(args.reference.read_text(encoding="utf-8"))
        if actual != expected:
            candidate = args.reference.with_suffix(".candidate.json")
            candidate.write_text(json.dumps(actual, ensure_ascii=False), encoding="utf-8")
            raise SystemExit("Result mismatch; candidate saved to " + str(candidate))
        print("PASS: all TOKENIZE output/frequencies and MATCH IDs/scores are identical")
    print("result_sha256:", hashlib.sha256(run.stdout).hexdigest())
    print("result_rows:", len(run.stdout.splitlines()))


if __name__ == "__main__":
    main()
