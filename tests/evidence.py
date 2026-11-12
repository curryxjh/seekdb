"""Strict validation of the sole learning artifact, hashes and RFC6901 pointers."""
import argparse
import hashlib
import json
from pathlib import Path
import re

from benchmark import source_digest


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: " + key)
        result[key] = value
    return result


def resolve(document, pointer):
    if pointer == "":
        return document
    assert pointer.startswith("/"), pointer
    value = document
    for part in pointer[1:].split("/"):
        assert not re.search(r"~(?![01])", part), pointer
        key = part.replace("~1", "/").replace("~0", "~")
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def run(path):
    raw = path.read_bytes()
    assert len(raw) <= 128 * 1024, len(raw)
    document = json.loads(
        raw.decode("utf-8"), object_pairs_hook=unique_object,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    assert isinstance(document, dict)
    assert document["metadata"]["source_sha256"] == source_digest()
    assert not re.search(r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
                         r"|sk-[A-Za-z0-9_-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)",
                         raw.decode("utf-8"))

    refs = 0

    def walk(value):
        nonlocal refs
        if isinstance(value, dict):
            for key, item in value.items():
                if key.endswith("_refs"):
                    for pointer in item:
                        resolve(document, pointer)
                        refs += 1
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(document)
    def check_excerpt(excerpt):
        original = resolve(document, excerpt["pointer"])
        assert isinstance(original, str) and 0 < len(excerpt["text"]) <= 240
        assert excerpt["text"] in original
    for excerpt in document.get("excerpts", []):
        check_excerpt(excerpt)
    used_evidence = {}
    for unit in document.get("learning_units", []):
        for field in ["student_evidence", "result_evidence"]:
            for excerpt in unit.get(field, []):
                check_excerpt(excerpt)
                identity = (excerpt["pointer"], excerpt["text"])
                previous = used_evidence.setdefault(identity, unit["id"])
                assert previous == unit["id"], "evidence reused across units"
                if field == "student_evidence":
                    match = re.fullmatch(r"/messages/(\d+)/content", excerpt["pointer"])
                    assert match and document["messages"][int(match[1])]["role"] == "student"
        student = {x["pointer"] for x in unit.get("student_evidence", [])}
        results = {x["pointer"] for x in unit.get("result_evidence", [])}
        assert not student & results, "student and result citations must be distinct"
    for message in document["messages"]:
        assert message["role"] in ["student", "agent", "tool"]
        assert isinstance(message["content"], str)
    for artifact in document["artifacts"]:
        content = Path(artifact["path"]).read_bytes()
        assert hashlib.sha256(content).hexdigest() == artifact["sha256"], artifact["path"]
    for experiment in document["experiments"]:
        if experiment.get("final_source"):
            assert experiment["source_sha256"] == source_digest(), experiment["id"]
    print(f"PASS strict UTF-8 JSON, {len(raw)} bytes, {refs} pointers, excerpts, "
          f"{len(document['artifacts'])} artifact hashes and final source digest")
    return document


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--file", type=Path, default=Path("agent/conversation.json"))
    run(p.parse_args().file)
