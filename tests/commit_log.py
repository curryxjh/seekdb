"""Recovery properties of the append-only commit record, using real processes.

Truncate every byte of the last footer; corrupt its checksum; append large
uncommitted tails and split magic strings across recovery chunk boundaries.
Optionally migrate a data directory created by the actual previous binary.
"""
import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile

from client import Client


def rows(binary, directory):
    client = Client([binary], directory)
    try:
        return client.ok("SELECT * FROM t ORDER BY id;")["rows"]
    finally:
        client.close()


def run(binary, legacy_binary=None):
    checks = 0
    with tempfile.TemporaryDirectory(prefix="mvdb-commit-log-") as temporary:
        root = Path(temporary)
        base = root / "base"
        client = Client([binary], base)
        client.ok("CREATE TABLE t(id BIGINT PRIMARY KEY,n BIGINT);")
        client.ok("INSERT INTO t VALUES(1,10);")
        client.close()
        before = (base / "data").read_bytes()
        client = Client([binary], base)
        client.ok("INSERT INTO t VALUES(2,20);")
        client.close()
        complete = (base / "data").read_bytes()
        assert complete.startswith(before)
        assert rows(binary, base) == [[1, 10], [2, 20]]
        checks += 1
        # Only the new footer is incomplete. Every prefix must recover the
        # old snapshot, rather than an empty database or partially updated row.
        for cut in range(1, 57):
            directory = root / f"truncated-{cut}"
            shutil.copytree(base, directory)
            (directory / "data").write_bytes(complete[:-cut])
            assert rows(binary, directory) == [[1, 10]], cut
            assert rows(binary, directory) == [[1, 10]], (cut, "idempotence")
            checks += 2
        directory = root / "corrupt-checksum"
        shutil.copytree(base, directory)
        damaged = bytearray(complete)
        damaged[-17] ^= 1
        (directory / "data").write_bytes(damaged)
        assert rows(binary, directory) == [[1, 10]]
        checks += 1
        directory = root / "missing-data-with-valid-footer"
        shutil.copytree(base, directory)
        damaged = bytearray(complete)
        damaged[len(before)] ^= 1
        (directory / "data").write_bytes(damaged)
        assert rows(binary, directory) == [[1, 10]]
        checks += 1
        # Force scanning several chunks. Bogus signatures and length fields
        # must not prevent recovery or allocate attacker-controlled lengths.
        for tail in [b"x" * (3 * 1024 * 1024),
                     (b"\xff" * 24 + b"MVDBCOMMIT000001") * 27000,
                     b"x" * (1024 * 1024 - 8) + b"MVDBCOMMIT000001"]:
            directory = root / f"tail-{checks}"
            shutil.copytree(base, directory)
            with (directory / "data").open("ab") as out:
                out.write(tail)
            assert rows(binary, directory) == [[1, 10], [2, 20]]
            assert (directory / "data").read_bytes() == complete
            checks += 1
        for point in ["after_commit_catalog", "after_commit_footer"]:
            directory = root / point
            shutil.copytree(base, directory)
            client = Client([binary], directory, {"MVDB_FAULT": point})
            client.send("INSERT INTO t VALUES(3,30);")
            try:
                client.receive()
                raise AssertionError("build needs FAULTS=1")
            except EOFError:
                pass
            client.process.wait(timeout=30)
            assert client.process.returncode == -9
            client.kill()
            actual = rows(binary, directory)
            expected = [[1, 10], [2, 20]]
            if point == "after_commit_catalog":
                assert actual == expected
            else:
                assert actual in [expected, expected + [[3, 30]]]
            checks += 1
        # A live transaction may flush far past the old footer, then be killed.
        client = Client([binary], base)
        client.ok("BEGIN;")
        client.ok("DELETE FROM t WHERE id=1;")
        client.kill()
        assert rows(binary, base) == [[1, 10], [2, 20]]
        checks += 1
        if legacy_binary:
            legacy = root / "legacy"
            client = Client([legacy_binary], legacy)
            client.ok("CREATE TABLE t(id BIGINT PRIMARY KEY,n BIGINT);")
            client.ok("INSERT INTO t VALUES(1,10);")
            client.close()
            assert rows(binary, legacy) == [[1, 10]]
            client = Client([binary], legacy)
            client.ok("INSERT INTO t VALUES(2,20);")
            client.close()
            assert rows(binary, legacy) == [[1, 10], [2, 20]]
            contents = (legacy / "data").read_bytes()
            downgrade = subprocess.run([legacy_binary, "--data", str(legacy)],
                                       input=b"", capture_output=True, timeout=30)
            assert downgrade.returncode != 0 and not downgrade.stdout
            assert (legacy / "data").read_bytes() == contents
            checks += 3
    print(f"PASS commit log: {checks} checks; torn footer, checksum, multi-chunk tails, "
          "new commit crash points, repeat recovery, uncommitted rollback and legacy migration")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True)
    parser.add_argument("--legacy-binary")
    args = parser.parse_args()
    run(str(Path(args.binary).resolve()),
        str(Path(args.legacy_binary).resolve()) if args.legacy_binary else None)
