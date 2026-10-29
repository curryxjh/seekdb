#!/usr/bin/env python3
"""Native file-table integration tests against a dedicated running seekdb.

Requires PyMySQL and PyArrow (fixture creation only). This test changes global
secure_file_priv temporarily; never run against a shared/production instance.
Example: python3 file_sql_regression.py --port 12991 --socket /instance/run/sql.sock
"""

import argparse
import datetime as dt
import os
from pathlib import Path
import tempfile
import unittest

import pymysql
import pyarrow as pa
import pyarrow.parquet as pq


class FileSqlRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.connection = pymysql.connect(host=ARGS.host, port=ARGS.port,
                                        unix_socket=ARGS.socket,
                                        user=ARGS.user, password=ARGS.password,
                                        charset="utf8mb4", autocommit=True)
        cls.temporary = tempfile.TemporaryDirectory(prefix="seekdb-file-sql-")
        cls.root = Path(cls.temporary.name).resolve()
        with cls.connection.cursor() as cur:
            cur.execute("SELECT @@GLOBAL.secure_file_priv")
            cls.old_root = cur.fetchone()[0]
            cur.execute("SET GLOBAL secure_file_priv = %s", (str(cls.root),))
            cur.execute("SET ob_query_timeout = 60000000")

    @classmethod
    def tearDownClass(cls):
        try:
            with cls.connection.cursor() as cur:
                cur.execute("SET GLOBAL secure_file_priv = %s", (cls.old_root,))
        finally:
            cls.connection.close()
            cls.temporary.cleanup()

    def setUp(self):
        self.query("SET SESSION enable_file_sql = ON")

    def query(self, sql, params=None):
        with self.connection.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def write(self, name, data):
        path = self.root / name
        path.write_bytes(data.encode("utf8") if isinstance(data, str) else data)
        return str(path)

    def reader(self, name, data, function="read_csv", options=""):
        path = self.write(name, data)
        return function + "(" + self.connection.escape(path) + options + ")"

    def error(self, sql):
        with self.assertRaises(pymysql.Error):
            self.query(sql)
        self.assertEqual(((1,),), self.query("SELECT 1"))

    def test_default_disabled_and_prepare_toggle(self):
        source = self.reader("switch.csv", "id\n1\n")
        self.query("SET enable_file_sql = DEFAULT")
        self.error("SELECT * FROM " + source)
        self.error("SELECT * FROM glob(" + self.connection.escape(str(self.root / "*.csv")) + ")")
        self.error("COPY (SELECT 1) TO " + self.connection.escape(str(self.root / "disabled.csv")))
        self.query("SET enable_file_sql = ON")
        self.query("PREPARE file_sql_statement FROM %s", ("SELECT * FROM " + source,))
        try:
            self.assertEqual(((1,),), self.query("EXECUTE file_sql_statement"))
            self.query("SET enable_file_sql = OFF")
            self.error("EXECUTE file_sql_statement")
        finally:
            self.query("DEALLOCATE PREPARE file_sql_statement")

    def test_csv_multiline_crlf_unicode_and_null(self):
        source = self.reader("complex.csv", 'id,text,empty,nil\r\n1,"中文,\r\nworld","",\r\n2,"say ""hi""",x,\r\n')
        self.assertEqual(((1, "中文,\r\nworld", "", None), (2, 'say "hi"', "x", None)),
                         self.query("SELECT * FROM " + source + " ORDER BY id"))
        source = self.reader("bom.csv", '\ufeff"id",text\r\n1,中文\r\n')
        self.assertEqual(((1, "中文"),), self.query("SELECT * FROM " + source))

    def test_csv_named_options_no_header_and_escape(self):
        source = self.reader("custom.csv", "1|'a\\'b'|NULL\n2|''|'NULL'\n",
                             options=", header=false, delimiter='|', quote=CHAR(39), escape=CHAR(92), nullstr='NULL'")
        # Constant expressions are intentionally not options: only SQL literals.
        self.error("SELECT * FROM " + source)
        source = "read_csv(%s, header=false, delimiter='|', quote=%s, escape=%s, nullstr='NULL')"
        rows = self.query("SELECT * FROM " + source, (str(self.root / "custom.csv"), "'", "\\"))
        self.assertEqual(((1, "a'b", None), (2, "", "NULL")), rows)

    def test_json_union_conflicts_missing_fields_and_alias(self):
        source = self.reader("union.jsonl", '{"id":1,"v":2}\n{"id":2,"v":2.5,"flag":true}\n{"id":3,"v":"three"}\n', "read_ndjson")
        self.assertEqual(((1, "2", None), (2, "2.5", 1), (3, "three", None)),
                         self.query("SELECT f.id,f.v,f.flag FROM " + source + " AS f ORDER BY f.id"))
        alias = source.replace("read_ndjson", "read_json")
        self.assertEqual(self.query("SELECT * FROM " + source), self.query("SELECT * FROM " + alias))

    def test_schema_full_inference_and_numeric_boundaries(self):
        source = self.reader("late.csv", "v\n" + "1\n" * 5000 + "wide\n")
        self.assertEqual(((5001, "wide"),), self.query("SELECT COUNT(*),MAX(v) FROM " + source))
        source = self.reader("types.csv", "i,d,b,day,ts,nil,overflow\n-9223372036854775808,1.5,true,2024-02-29,2024-02-29T01:02:03,,9223372036854775808\n9223372036854775807,2,false,2025-01-01,2025-01-01T00:00:00,,18446744073709551615\n")
        rows = self.query("SELECT * FROM " + source + " ORDER BY i")
        self.assertEqual(-9223372036854775808, rows[0][0])
        self.assertEqual(9223372036854775807, rows[1][0])
        self.assertEqual(dt.date(2024, 2, 29), rows[0][3])
        self.assertEqual(dt.datetime(2024, 2, 29, 1, 2, 3), rows[0][4])
        self.assertIsNone(rows[0][5])
        self.assertEqual("18446744073709551615", rows[1][6])
        description = self.query("DESCRIBE SELECT * FROM " + source)
        self.assertEqual(["i", "d", "b", "day", "ts", "nil", "overflow"], [r[0] for r in description])
        for row, typename in zip(description, ("bigint", "double", "boolean", "date", "datetime")):
            self.assertIn(typename, row[1].lower())
        self.error("SELECT * FROM " + source[:-1] + ", sample_size=10)")

    def test_native_filter_aggregate_order_expression_limit(self):
        source = self.reader("aggregate.csv", "id,g,score\n1,a,10\n2,a,20\n3,b,30\n4,b,40\n")
        self.assertEqual((("b", 2, 70, 35.0, 30, 40),), self.query(
            "SELECT g,COUNT(*),SUM(score),AVG(score),MIN(score),MAX(score) FROM " + source +
            " WHERE id>1 GROUP BY g HAVING COUNT(*)>1 ORDER BY g LIMIT 1"))
        self.assertEqual(((8, "b"), (6, "b")), self.query(
            "SELECT id*2 AS doubled,g FROM " + source + " WHERE score>=30 ORDER BY doubled DESC LIMIT 2"))
        plan = "\n".join(str(r) for r in self.query("EXPLAIN SELECT * FROM " + source))
        self.assertIn("FUNCTION", plan.upper())

    def parquet(self, name="groups.parquet", **kwargs):
        table = pa.table({"id": pa.array([1, 2, 3, 4], pa.int64()),
                          "value": pa.array([1.5, None, 3.5, 4.5], pa.float64()),
                          "text": ["中文", "two", None, "four"],
                          "flag": [True, False, None, True],
                          "day": pa.array([dt.date(2024, 2, 29), None, dt.date(1970, 1, 1), dt.date(2000, 1, 1)], pa.date32()),
                          "ts": pa.array([dt.datetime(1969, 12, 31, 23, 59, 59), None, dt.datetime(2024, 1, 1), dt.datetime(2000, 1, 1)], pa.timestamp("us"))})
        path = self.root / name
        pq.write_table(table, path, row_group_size=2, compression="snappy", **kwargs)
        return "read_parquet(" + self.connection.escape(str(path)) + ")"

    def test_parquet_multiple_groups_nullable_primitive_projection(self):
        for version in ("1.0", "2.0"):
            source = self.parquet("v" + version + ".parquet", data_page_version=version)
            self.assertEqual(((4, 9.5),), self.query("SELECT COUNT(*),SUM(value) FROM " + source))
            rows = self.query("SELECT id,text,flag,day,ts FROM " + source + " ORDER BY id")
            self.assertEqual((1, "中文", 1, dt.date(2024, 2, 29), dt.datetime(1969, 12, 31, 23, 59, 59)), rows[0])
            self.assertEqual((2, "two", 0, None, None), rows[1])
            self.assertEqual(((4,), (3,)), self.query("SELECT id FROM " + source + " ORDER BY id DESC LIMIT 2"))

    def test_parquet_unsigned_boundary(self):
        path = self.root / "unsigned.parquet"
        pq.write_table(pa.table({"u32": pa.array([4294967295], pa.uint32()),
                                 "u64": pa.array([18446744073709551615], pa.uint64())}), path)
        self.assertEqual(((4294967295, "18446744073709551615"),), self.query("SELECT * FROM read_parquet(%s)", (str(path),)))

    def test_inner_left_cross_format_join_and_rescan(self):
        left = self.reader("left.csv", "id,name\n1,one\n2,two\n3,three\n")
        right = self.reader("right.jsonl", '{"id":2,"score":20}\n{"id":3,"score":30}\n', "read_json")
        self.assertEqual(((2, "two", 20), (3, "three", 30)), self.query(
            "SELECT l.id,l.name,r.score FROM " + left + " l INNER JOIN " + right + " r ON l.id=r.id ORDER BY l.id"))
        self.assertEqual(((1, None), (2, 20), (3, 30)), self.query(
            "SELECT /*+ USE_NL(l r) */ l.id,r.score FROM " + left + " l LEFT JOIN " + right + " r ON l.id=r.id ORDER BY l.id"))
        parquet = self.parquet("join.parquet")
        self.assertEqual(((1, "中文"), (2, "two"), (3, None)), self.query(
            "SELECT l.id,r.text FROM " + left + " l JOIN " + parquet + " r ON l.id=r.id ORDER BY l.id"))

    def test_schema_changes_are_not_hidden_by_plan_cache(self):
        source = self.reader("changing.csv", "id\n1\n")
        statement = "SELECT * FROM " + source
        self.assertEqual(((1,),), self.query(statement))
        self.write("changing.csv", "id,text\n2,fresh\n")
        self.assertEqual(((2, "fresh"),), self.query(statement))
        description = self.query("DESCRIBE " + statement)
        self.assertEqual(["id", "text"], [column[0] for column in description])
        self.query("PREPARE changing_file FROM %s", (statement,))
        try:
            self.write("changing.csv", "id,text\n3,next\n")
            try:
                rows = self.query("EXECUTE changing_file")
            except pymysql.Error:
                self.assertEqual(((1,),), self.query("SELECT 1"))
            else:
                self.assertEqual(((3, "next"),), rows)
        finally:
            self.query("DEALLOCATE PREPARE changing_file")

    def test_many_rows_and_glob_metadata(self):
        source = self.reader("many.csv", "id\n" + "".join(str(i) + "\n" for i in range(3001)))
        self.assertEqual(((3001, 4501500),), self.query("SELECT COUNT(*),SUM(id) FROM " + source))
        a = self.write("find-a.csv", "id\n1\n")
        b = self.write("find-b.csv", "id\n2\n")
        self.assertEqual(((a,), (b,)), self.query("SELECT file FROM glob(%s) ORDER BY file", (str(self.root / "find-*.csv"),)))
        rows = self.query("DESCRIBE SELECT * FROM glob(%s)", (str(self.root / "find-*.csv"),))
        self.assertEqual("file", rows[0][0])

    def test_copy_roundtrip_custom_options_and_no_overwrite(self):
        source = self.reader("copy-in.csv", 'id,text,empty,nil\n1,"中文,\nline","",\n2,"NULL","",\n')
        path = str(self.root / "copy-out.csv")
        self.query("COPY (SELECT * FROM " + source + " ORDER BY id) TO %s (HEADER, DELIMITER '|', NULLSTR 'NULL')", (path,))
        self.assertEqual(self.query("SELECT * FROM " + source + " ORDER BY id"), self.query(
            "SELECT * FROM read_csv(%s, delimiter='|', nullstr='NULL') ORDER BY id", (path,)))
        original = Path(path).read_bytes()
        self.error("COPY (SELECT 1) TO " + self.connection.escape(path))
        self.assertEqual(original, Path(path).read_bytes())

    def test_copy_table_and_ordinary_sql_still_work(self):
        database = "file_sql_regression_" + str(os.getpid())
        self.query("CREATE DATABASE " + database)
        try:
            self.query("CREATE TABLE " + database + ".items(id BIGINT PRIMARY KEY, name VARCHAR(40))")
            self.query("INSERT INTO " + database + ".items VALUES (1,'one'),(2,'中文')")
            self.assertEqual(((3,),), self.query("SELECT SUM(id) FROM " + database + ".items"))
            self.assertEqual("id", self.query("DESCRIBE " + database + ".items")[0][0])
            path = str(self.root / "table-copy.csv")
            self.query("COPY " + database + ".items TO %s (HEADER)", (path,))
            self.assertEqual(((1, "one"), (2, "中文")), self.query(
                "SELECT * FROM read_csv(%s) ORDER BY id", (path,)))
            values = ",".join(f"({i},'row')" for i in range(3, 1101))
            self.query("INSERT INTO " + database + ".items VALUES " + values)
            path = str(self.root / "table-filtered-copy.csv")
            self.query("COPY (SELECT id*2 AS doubled FROM " + database +
                       ".items WHERE MOD(id,3)=0 ORDER BY id DESC) TO %s (HEADER)", (path,))
            self.assertEqual(tuple((i*2,) for i in range(1098, 0, -3)), self.query(
                "SELECT doubled FROM read_csv(%s)", (path,)))
        finally:
            self.query("DROP DATABASE " + database)

    def test_copy_literal_options_after_parameterization(self):
        # String and numeric option literals pass through SQL parameterization.
        # Exercise them independently so failures cannot hide behind a query error.
        for index, options in enumerate((
                "HEADER 0, DELIMITER '|', NULLSTR ''",
                "FORMAT 'csv', HEADER 1, QUOTE '\"', ESCAPE '\"', NULLSTR 'nil'")):
            path = str(self.root / f"literal-options-{index}.csv")
            self.query("COPY (SELECT 'a|b' AS text, NULL AS missing, '' AS empty) TO %s (" +
                       options + ")", (path,))
            content = Path(path).read_text()
            self.assertIn('"a|b"', content)
            self.assertIn('""', content)
            if index == 0:
                self.assertEqual('"a|b"||""\n', content)
                self.assertEqual((("a|b", None, ""),), self.query(
                    "SELECT * FROM read_csv(%s, header=false, delimiter='|', nullstr='')", (path,)))
            else:
                self.assertIn(',nil,', content)
                self.assertTrue(content.startswith('"text","missing","empty"\n'))
        for index, options in enumerate(("unknown true", "HEADER 2", "DELIMITER '||'",
                                         "NULLSTR 'a,b'", "HEADER, HEADER false")):
            path = str(self.root / f"invalid-options-{index}.csv")
            self.error("COPY (SELECT 1) TO " + self.connection.escape(path) + " (" + options + ")")
            self.assertFalse(Path(path).exists())

    def test_copy_query_failure_cleans_temporary(self):
        source = self.reader("copy-error.csv", "id\n0\n1\n2\n")
        path = str(self.root / "never-publish.csv")
        before = set(self.root.glob(".seekdb-copy-*"))
        self.error("COPY (SELECT id+9223372036854775807 FROM " + source + ") TO " + self.connection.escape(path))
        self.assertFalse(Path(path).exists())
        self.assertEqual(before, set(self.root.glob(".seekdb-copy-*")))

    def test_paths_special_files_and_feature_security(self):
        for path in ("/etc/passwd", str(self.root / ".." / "outside.csv")):
            self.error("SELECT * FROM read_csv(" + self.connection.escape(path) + ")")
        link = self.root / "outside-link.csv"
        link.symlink_to("/etc/passwd")
        self.error("SELECT * FROM read_csv(" + self.connection.escape(str(link)) + ")")
        fifo = self.root / "pipe.csv"
        os.mkfifo(fifo)
        self.error("SELECT * FROM read_csv(" + self.connection.escape(str(fifo)) + ")")
        source = self.reader("secure.csv", "id\n1\n")
        try:
            self.query("SET GLOBAL secure_file_priv = ''")
            self.error("SELECT * FROM " + source)
            self.error("COPY (SELECT 1) TO " + self.connection.escape(str(self.root / "denied.csv")))
        finally:
            self.query("SET GLOBAL secure_file_priv = %s", (str(self.root),))

    def test_malformed_inputs_and_options_keep_connection_healthy(self):
        for i, text in enumerate(('a,b\n1\n', 'a\n"unfinished\n', 'a\n"quoted"x\n')):
            self.error("SELECT * FROM " + self.reader(f"bad-{i}.csv", text))
        for i, text in enumerate(('{"a":{}}\n', '{"a":[]}\n', '{"a":1,"a":2}\n', '{"a":}\n')):
            self.error("SELECT * FROM " + self.reader(f"bad-{i}.jsonl", text, "read_json"))
        self.error("SELECT * FROM " + self.reader("bad.parquet", b"PAR1brokenPAR1", "read_parquet"))
        self.error("SELECT * FROM " + self.reader("utf8.csv", b"a\n\xff\n"))
        source = self.reader("bad-options.csv", "a\n1\n")
        for option in (", unknown=true)", ", header=true, header=false)", ", delimiter='||')"):
            self.error("SELECT * FROM " + source[:-1] + option)
        self.assertEqual(((3,),), self.query("SELECT 1+2"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--socket", help="server Unix socket; required for changing secure_file_priv")
    parser.add_argument("--user", default="root")
    parser.add_argument("--password", default=os.getenv("SEEKDB_TEST_PASSWORD", ""))
    ARGS, rest = parser.parse_known_args()
    unittest.main(argv=[__file__] + rest, verbosity=2)
