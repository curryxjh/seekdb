"""Supplementary assertions over official JSONL; protocol_check/run.py is authoritative."""
import argparse
import json
import math
from pathlib import Path
import tempfile

from client import Client


def equal(actual, expected):
    if isinstance(expected, float):
        return isinstance(actual, (float, int)) and math.isclose(
            actual, expected, abs_tol=1e-5, rel_tol=1e-5)
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(
            equal(a, e) for a, e in zip(actual, expected))
    return type(actual) is type(expected) and actual == expected


def assertion(result, expected):
    if "error" in expected:
        assert result.get("ok") is False and result.get("error"), (result, expected)
        return
    assert result.get("ok") is True, result
    for field in ["affected_rows", "columns", "rows"]:
        if field not in expected:
            continue
        actual = result[field]
        if field == "rows" and expected.get("unordered"):
            actual = list(actual)
            for row in expected[field]:
                match = next((i for i, candidate in enumerate(actual) if equal(candidate, row)), None)
                assert match is not None, (result, expected)
                actual.pop(match)
            assert not actual, (result, expected)
        else:
            assert equal(actual, expected[field]), (field, actual, expected[field])


def run(command, package, temp_root=None):
    manifest = json.loads((package / "manifest.json").read_text())
    total = 0
    for case in manifest["cases"]:
        with tempfile.TemporaryDirectory(dir=temp_root, prefix="mvdb-public-") as directory:
            client = Client(command, directory)
            results = json.loads((package / case["result"]).read_text())
            position = 0
            try:
                for line in (package / case["test"]).read_text().splitlines():
                    if not line.strip():
                        continue
                    if line == "--restart":
                        client.close()
                        client = Client(command, directory)
                    elif line.startswith("--mode "):
                        client.mode = line[7:]
                    else:
                        assertion(client.sql(line), results[position])
                        position += 1
                assert position == len(results) == case["statements"]
                total += position
            finally:
                client.close()
        print(f"PASS {case['id']} ({position} SQL)")
    print(f"PASS public: {len(manifest['cases'])} cases, {total} SQL, {manifest['version']}; official JSONL")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--binary", required=True)
    p.add_argument("--package", type=Path, default=Path("mysqltest"))
    p.add_argument("--temp-root")
    args = p.parse_args()
    run([args.binary], args.package, args.temp_root)
