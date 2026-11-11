"""Exercise boundary cases through the unchanged official Session transport."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "protocol_check"))
from _transport import Session


def run(binary=None, image=None):
    config = json.loads((ROOT / "protocol_check/config.json").read_text())
    command = [str(Path(binary).resolve()), "--data", "{data_dir}"] if binary else None
    with tempfile.TemporaryDirectory() as tmp:
        session = Session(config, Path(tmp) / "stderr", image=image, command=command)
        try:
            session.start()
            assert session.request("CREATE TABLE t(id BIGINT PRIMARY KEY,s VARCHAR(64));")["ok"]
            for ident in [2**80 + 123, -(2**80), "中文😀;\n\"\\", 1.25, True, [1, "a"], {"n": 2**63}]:
                result = session.request("SELECT id FROM t;", ident=ident)
                assert result["ok"] and result["rows"] == [], result
            session.send({"id": None, "op": "execute", "sql": "SELECT id FROM t;"})
            assert session.read(60) == {"id": None, "ok": True, "columns": ["id"], "rows": []}
            sql = "/* ; */ INSERT INTO t VALUES\n(9223372036854775807,'中文😀;\\\"\nnext');"
            assert session.request(sql)["affected_rows"] == 1
            result = session.request("SELECT * FROM t;")
            assert result["rows"] == [[2**63 - 1, '中文😀;"\nnext']], result
            result = session.request("INSERT INTO t VALUES(9223372036854775807,'duplicate');")
            assert result["ok"] is False and result["error"]["code"] == "duplicate_key", result
            assert session.request("SELECT id FROM t;", mode="ann")["rows"] == [[2**63 - 1]]
            for request in [
                b'{"id":1,"op":"execute","sql":"DELETE FROM t;","sql":"SELECT id FROM t;"}',
                b'{"id":1,"op":"execute","sql":"DELETE FROM t;","extra":{"x":0,"x":1}}',
                b'{"id":NaN,"op":"execute","sql":"DELETE FROM t;"}',
                b'{"id":Infinity,"op":"execute","sql":"DELETE FROM t;"}',
                b'{"id":01,"op":"execute","sql":"DELETE FROM t;"}',
                b'{"id":1e999,"op":"execute","sql":"DELETE FROM t;"}',
                b'{"id":1,"op":"execute","sql":"\\ud800"}',
                b'{"id":1,"op":"execute","sql":"\\udc00"}',
                b'{"id":1,"op":"execute","sql":"\xed\xa0\x80"}',
                b'[]', b'{}', b'{"id":1,"op":"execute","sql":12}',
            ]:
                payload = request + b"\n"
                # Split a physical request to exercise streaming pipe input.
                for offset in range(0, len(payload), 7):
                    os.write(session.proc.stdin.fileno(), payload[offset:offset+7])
                result = session.read(60)
                assert result["ok"] is False and result["error"]["code"], (request, result)
                assert session.request("SELECT id FROM t;")["rows"] == [[2**63 - 1]]
            assert not session.request("DELETE FROM t; SELECT id FROM t;")["ok"]
            assert session.request("SELECT id FROM t;")["rows"] == [[2**63 - 1]]
            assert session.request("BEGIN;")["ok"]
            assert session.request("DELETE FROM t;")["affected_rows"] == 1
            session.shutdown()
            session.start()
            assert session.request("SELECT id FROM t;")["rows"] == [[2**63 - 1]]
            session.shutdown()
        finally:
            session.close()
        stderr = (Path(tmp) / "stderr").read_text()
        if image:
            # Docker CLI warns when this arm64 host runs the required amd64 image.
            stderr = "\n".join(line for line in stderr.splitlines() if line !=
                "WARNING: The requested image's platform (linux/amd64) does not match the detected host platform (linux/arm64/v8) and no specific platform was requested")
        assert not stderr, stderr
    print("PASS official transport: ID types/precision, UTF-8/escapes, fragmented input, strict JSON, "
          "one response, SQL errors, modes, atomic rejection, shutdown rollback and restart")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--binary")
    target.add_argument("--image")
    args = p.parse_args()
    run(args.binary, args.image)
