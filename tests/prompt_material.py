"""Check material integrity against the scored baseline; no score estimation.

Run from a clean LF export (see docs/prompt-material.md). All checks use stdlib.
"""
import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import unittest

import evidence

BASELINE = "12b8554683f8f3208e46d74ccf048d76a592c102"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def strict_load(raw):
    require(len(raw) <= 131072, "material exceeds 128 KiB")

    def finite(text):
        value = float(text)
        require(math.isfinite(value), "nonfinite JSON number")
        return value

    def constant(text):
        raise ValueError("nonfinite JSON constant: " + text)

    value = json.loads(raw.decode("utf-8"), object_pairs_hook=evidence.unique_object,
                       parse_float=finite, parse_constant=constant)
    require(isinstance(value, dict), "root must be an object")
    return value


def pointer(document, ref):
    require(isinstance(ref, str), "pointer must be a string")
    if ref == "":
        return document
    require(ref.startswith("/"), "pointer must begin with /")
    value = document
    for part in ref[1:].split("/"):
        require(not re.search(r"~(?![01])", part), "invalid pointer escape")
        key = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            require(bool(re.fullmatch(r"0|[1-9][0-9]*", key)), "invalid array index")
            value = value[int(key)]
        else:
            require(isinstance(value, dict), "pointer crosses a scalar")
            value = value[key]
    return value


def check_references(document):
    count = 0

    def walk(value):
        nonlocal count
        if isinstance(value, dict):
            for key, item in value.items():
                if key.endswith("_refs"):
                    require(isinstance(item, list), "references must be arrays")
                    for ref in item:
                        pointer(document, ref)
                        count += 1
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(document)
    for item in document.get("excerpts", []):
        target = pointer(document, item["pointer"])
        quote = item["text"]
        require(isinstance(target, str) and isinstance(quote, str), "quote target type")
        require(0 < len(quote) <= 240 and quote in target, "quote not exact or too long")
    return count


def check_preservation(current, baseline):
    """Expand shared context before comparing inherited records to the merge source."""
    require(current["messages"][:len(baseline["messages"])] == baseline["messages"],
            "inherited messages changed")
    for key in ["engineering_decisions", "learning_units", "learning_opportunities",
                "artifacts", "remaining_limits"]:
        require(current[key] == baseline[key], "inherited " + key + " changed")
    require(current["excerpts"][:len(baseline["excerpts"])] == baseline["excerpts"],
            "inherited excerpts changed")
    require(len(current["experiments"]) == len(baseline["experiments"]),
            "inherited experiments removed or padded")
    changed = []
    for index, (now, old) in enumerate(zip(current["experiments"], baseline["experiments"])):
        restored = copy.deepcopy(now)
        for field in ["environment", "limitations", "scope", "working_directory_note"]:
            refs = restored.pop(field + "_refs", None)
            if refs is not None:
                require(field not in restored and len(refs) == 1,
                        "ambiguous experiment context")
                restored[field] = pointer(current, refs[0])
                require(isinstance(restored[field], str), "context must be a string")
        if now.get("output") != old.get("output"):
            # Parse with duplicate-key and finite-number checks before comparison.
            require(json.dumps(strict_load(now["output"].encode()), sort_keys=True) ==
                    json.dumps(strict_load(old["output"].encode()), sort_keys=True),
                    "experiment output values changed")
            restored["output"] = old["output"]
            changed.append(index)
        require(restored == old, "inherited experiment changed: " + str(index))
    require(current["metadata"]["source_sha256"] == baseline["metadata"]["source_sha256"],
            "database source digest changed")
    for key, old in baseline["metadata"].items():
        if key != "delivery_branch":
            require(current["metadata"][key] == old, "historical metadata changed: " + key)
    return changed


class InputChecks(unittest.TestCase):
    def test_invalid_json(self):
        for raw in [b'{"a":1,"a":2}', b'{"x":NaN}', b'{"x":Infinity}',
                    b'{"x":1e309}', b'[]', b'\xff', b'{} {}', b'\xef\xbb\xbf{}',
                    b'{"x":1}' + b' ' * 131072]:
            with self.subTest(raw=raw[:30]), self.assertRaises((ValueError, UnicodeError)):
                strict_load(raw)

    def test_pointer_escaping(self):
        self.assertEqual(pointer({"a/b": {"~": [7]}}, "/a~1b/~0/0"), 7)
        self.assertEqual(pointer({"": 3}, "/"), 3)
        for ref in ["/a/-1", "/a/00", "/a/+0", "/a/-", "/a/~2"]:
            with self.subTest(ref=ref), self.assertRaises((ValueError, KeyError)):
                pointer({"a": [7]}, ref)

    def test_quotes_and_refs(self):
        self.assertEqual(check_references({"x": "observed", "result_refs": ["/x"],
                         "excerpts": [{"pointer": "/x", "text": "served"}]}), 1)
        with self.assertRaises(ValueError):
            check_references({"x": "observed", "excerpts": [{"pointer": "/x", "text": "fake"}]})

    def test_baseline_guard(self):
        old = {key: [] for key in ["messages", "experiments", "engineering_decisions",
               "learning_units", "learning_opportunities", "artifacts", "remaining_limits", "excerpts"]}
        old["metadata"] = {"source_sha256": "source", "delivery_branch": "old"}
        old["experiments"] = [{"output": '{ "passed": false }'}]
        now = copy.deepcopy(old)
        now["experiments"][0]["output"] = '{"passed":false}'
        self.assertEqual(check_preservation(now, old), [0])
        now["experiments"][0]["output"] = '{"passed":true}'
        with self.assertRaises(ValueError):
            check_preservation(now, old)
        now["experiments"][0]["output"] = '{"passed":0}'
        with self.assertRaises(ValueError):
            check_preservation(now, old)

    def test_shared_context_guard(self):
        old = {key: [] for key in ["messages", "experiments", "engineering_decisions",
               "learning_units", "learning_opportunities", "artifacts", "remaining_limits", "excerpts"]}
        old["metadata"] = {"source_sha256": "source"}
        old["experiments"] = [{"output": "unchanged", "environment": "real environment"}]
        now = copy.deepcopy(old)
        now["contexts"] = ["real environment"]
        del now["experiments"][0]["environment"]
        now["experiments"][0]["environment_refs"] = ["/contexts/0"]
        self.assertEqual(check_preservation(now, old), [])
        now["contexts"][0] = "different environment"
        with self.assertRaises(ValueError):
            check_preservation(now, old)


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--file", type=Path, default=Path("agent/conversation.json"))
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        result = unittest.TextTestRunner(verbosity=2).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(InputChecks))
        if not result.wasSuccessful():
            raise SystemExit(1)
    raw = args.file.read_bytes()
    document = strict_load(raw)
    original = (args.baseline.read_bytes() if args.baseline else subprocess.check_output(
        ["git", "show", BASELINE + ":agent/conversation.json"]))
    changed = check_preservation(document, strict_load(original))
    refs = check_references(document)
    evidence.run(args.file)
    report = {"scope": "local material validation; no platform score or admission claim",
              "passed": True, "baseline_commit": BASELINE, "bytes": len(raw),
              "sha256": hashlib.sha256(raw).hexdigest(),
              "baseline_material_sha256": hashlib.sha256(original).hexdigest(),
              "source_sha256": document["metadata"]["source_sha256"],
              "messages": len(document["messages"]), "experiments": len(document["experiments"]),
              "artifact_hashes": len(document["artifacts"]), "references": refs,
              "json_output_whitespace_only_indices": changed,
              "inherited_records_preserved": True}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
