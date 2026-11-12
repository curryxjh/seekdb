"""Drained uncommitted bytes must not become visible after kill or failure."""
import argparse
from pathlib import Path
import tempfile

from client import Client, sql_string


def run(binary, fault_writeback=False):
    payload = "x" * 4096
    with tempfile.TemporaryDirectory(prefix="mvdb-large-txn-") as directory:
        c = Client([binary], directory, timeout=180)
        try:
            c.ok("CREATE TABLE t(id BIGINT PRIMARY KEY,s VARCHAR(5000));")
            c.ok("INSERT INTO t VALUES(-1,'seed');")
            if fault_writeback:
                c.close()
                c = Client([binary], directory, env={"MVDB_FAULT": "after_early_writeback"}, timeout=180)

            def insert_large():
                for start in range(0, 6000, 250):
                    values = [f"({i},{sql_string(payload)})" for i in range(start, start+250)]
                    c.ok("INSERT INTO t VALUES " + ",".join(values) + ";")

            c.ok("BEGIN;")
            if fault_writeback:
                try:
                    insert_large()
                except EOFError:
                    c.kill()
                else:
                    raise AssertionError("early writeback fault did not execute")
                print("PASS deterministic kill at after_early_writeback, before catalog publication", flush=True)
            else:
                insert_large()
                assert len(c.ok("SELECT id FROM t;")["rows"]) == 6001
                c.kill()
            c = Client([binary], directory, timeout=180)
            assert c.ok("SELECT id,s FROM t;")["rows"] == [[-1, "seed"]]
            print("PASS large uncommitted insert: " + ("early sync confirmed" if fault_writeback else ">24 MiB written") +
                  "; SIGKILL -> only seed visible", flush=True)
            c.ok("BEGIN;")
            insert_large()
            c.ok("COMMIT;")
            c.close()
            c = Client([binary], directory, timeout=180)
            assert len(c.ok("SELECT id FROM t;")["rows"]) == 6001
            assert c.ok("SELECT s FROM t WHERE id=5999;")["rows"] == [[payload]]
            print("PASS large commit: 6000 durable rows and original marker after restart", flush=True)

            c.ok("BEGIN;")
            c.ok("UPDATE t SET s='kept' WHERE id=-1;")
            values = [f"({i},{sql_string(payload)})" for i in range(10000, 12100)]
            values.append("(-1,'duplicate')")
            failed = c.sql("INSERT INTO t VALUES " + ",".join(values) + ";")
            assert not failed["ok"] and failed["error"]["code"] == "duplicate_key"
            c.ok("COMMIT;")
            c.close()
            c = Client([binary], directory, timeout=180)
            assert c.ok("SELECT id FROM t WHERE id>=10000;")["rows"] == []
            assert c.ok("SELECT s FROM t WHERE id=-1;")["rows"] == [["kept"]]
            print("PASS failed >8 MiB statement: no partial rows, prior statement commits", flush=True)
        finally:
            c.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--binary", required=True)
    p.add_argument("--fault-writeback", action="store_true")
    args = p.parse_args()
    run(str(Path(args.binary).resolve()), args.fault_writeback)
