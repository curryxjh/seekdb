#!/usr/bin/env python3
"""CPU-only contract tests for verify_bridge.py, never a GPU/SQL benchmark.

Compiles a mock C ABI in a temporary directory. No production bridge, remote
instance, organizer library, or pre-existing snapshot is modified.
Usage: python3 -I tools/hipvs_bridge/test_verify_bridge.py
"""
import array
import json
import math
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest


MOCK_SOURCE = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct { long n, dim; float *base; } Index;
typedef struct { uint32_t id; double distance; } Result;
static int mode(const char *value) {
  const char *m = getenv("MOCK_MODE");
  return m && strcmp(m, value) == 0;
}
static void trace(const char *operation, long nq) {
  const char *path = getenv("MOCK_TRACE");
  if (!path) return;
  FILE *f = fopen(path, "a");
  if (f) { fprintf(f, "%s %ld\n", operation, nq); fclose(f); }
}
void *seekdb_cuvs_build(const float *base, long n, long dim) {
  trace("build", n);
  if (mode("no_build") || mode("null_build")) return NULL;
  Index *index = (Index *)calloc(1, sizeof(Index));
  if (!index) return NULL;
  index->base = (float *)malloc(sizeof(float) * n * dim);
  if (!index->base) { free(index); return NULL; }
  index->n = n; index->dim = dim;
  memcpy(index->base, base, sizeof(float) * n * dim);
  return index;
}
static int compare(const void *left, const void *right) {
  const Result *a = (const Result *)left, *b = (const Result *)right;
  if (a->distance < b->distance) return -1;
  if (a->distance > b->distance) return 1;
  return a->id < b->id ? -1 : a->id > b->id;
}
int seekdb_cuvs_search(void *handle, const float *query, long nq, long k,
                       uint32_t *ids, float *distances) {
  Index *index = (Index *)handle;
  trace("search", nq);
  if (!index || nq != 1 || k > index->n || mode("search_error")) return 15;
  if (mode("unwritten")) return 0;
  Result *results = (Result *)malloc(sizeof(Result) * index->n);
  if (!results) return 12;
  for (long i = 0; i < index->n; ++i) {
    results[i].id = (uint32_t)i;
    results[i].distance = 0;
    for (long d = 0; d < index->dim; ++d) {
      double delta = (double)query[d] - index->base[i * index->dim + d];
      results[i].distance += delta * delta;
    }
  }
  qsort(results, index->n, sizeof(Result), compare);
  for (long j = 0; j < k; ++j) {
    if (mode("partial") && j == k - 1) break;
    ids[j] = results[j].id;
    distances[j] = (float)results[j].distance;
  }
  if (mode("duplicate")) ids[k - 1] = ids[0];
  if (mode("invalid")) ids[k - 1] = (uint32_t)index->n;
  if (mode("nonfinite")) distances[k - 1] = __builtin_nanf("");
  if (mode("negative")) distances[0] = -1.0f;
  if (mode("wrong_distance")) distances[0] += 100.0f;
  if (mode("unsorted")) {
    float value = distances[0];
    distances[0] = distances[k - 1]; distances[k - 1] = value;
  }
  free(results);
  return 0;
}
void seekdb_cuvs_free(void *handle) {
  trace("free", 0);
  if (handle) { Index *index = (Index *)handle; free(index->base); free(index); }
}
int seekdb_cuvs_serialize(void *handle, const char *path) {
  trace("serialize", 0);
  if (mode("serialize_error")) return 1;
  Index *index = (Index *)handle;
  FILE *f = fopen(path, "wb");
  if (!f) return 1;
  int ok = fwrite(&index->n, sizeof(long), 1, f) == 1 &&
           fwrite(&index->dim, sizeof(long), 1, f) == 1 &&
           fwrite(index->base, sizeof(float), index->n * index->dim, f) ==
               (size_t)(index->n * index->dim);
  int close_rc = fclose(f);
  return ok && close_rc == 0 ? 0 : 1;
}
void *seekdb_cuvs_deserialize(const char *path, long n, long dim) {
  trace("deserialize", 0);
  FILE *f = fopen(path, "rb");
  if (!f) return NULL;
  long actual_n = 0, actual_dim = 0;
  if (fread(&actual_n, sizeof(long), 1, f) != 1 ||
      fread(&actual_dim, sizeof(long), 1, f) != 1 ||
      actual_n != n || actual_dim != dim) { fclose(f); return NULL; }
  Index *index = (Index *)calloc(1, sizeof(Index));
  if (!index) { fclose(f); return NULL; }
  index->n = n; index->dim = dim;
  index->base = (float *)malloc(sizeof(float) * n * dim);
  if (!index->base || fread(index->base, sizeof(float), n * dim, f) != (size_t)(n * dim)) {
    fclose(f); seekdb_cuvs_free(index); return NULL;
  }
  fclose(f);
  return index;
}
'''


class VerifierContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="seekdb-verifier-contract-")
        cls.root = Path(cls.temporary.name)
        cls.verifier = Path(__file__).with_name("verify_bridge.py").resolve()
        source = cls.root / "mock.c"
        source.write_text(MOCK_SOURCE)
        cls.library = cls.root / ("mock.dylib" if sys.platform == "darwin" else "mock.so")
        compiler = shlex.split(os.environ.get("CC", "cc"))
        shared = ["-dynamiclib"] if sys.platform == "darwin" else ["-shared", "-fPIC"]
        try:
            subprocess.run(compiler + ["-std=c11", "-O1", "-Wall", "-Wextra", "-Werror"]
                           + shared + [str(source), "-o", str(cls.library)], check=True)
        except BaseException:
            cls.temporary.cleanup()
            raise
        cls.base = array.array("f", (v for i in range(160) for v in (i * 0.125, (i % 3) * 0.25)))
        cls.queries = array.array("f", (v for i in (0, 43, 91, 159)
                                       for v in (cls.base[i * 2], cls.base[i * 2 + 1])))
        cls.truth = array.array("i")
        for qi in range(4):
            distances = [sum((cls.queries[qi * 2 + d] - cls.base[i * 2 + d]) ** 2
                             for d in range(2)) for i in range(160)]
            cls.truth.extend(sorted(range(160), key=lambda i: (distances[i], i))[:128])
        for name, values in (("base.f32", cls.base), ("query.f32", cls.queries), ("gt.i32", cls.truth)):
            with (cls.root / name).open("wb") as stream:
                values.tofile(stream)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def run_verifier(self, mode="exact", extra=(), base=None, queries=None, gt=None):
        with tempfile.TemporaryDirectory(prefix="case-", dir=self.root) as directory:
            trace = Path(directory) / "trace.txt"
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith(("MOCK_", "SEEKDB_CUVS_"))}
            environment.update(MOCK_MODE=mode, MOCK_TRACE=str(trace))
            command = [sys.executable, "-I", str(self.verifier), "--library", str(self.library),
                       "--base", str(base or self.root / "base.f32"),
                       "--queries", str(queries or self.root / "query.f32"),
                       "--gt", str(gt or self.root / "gt.i32"), "--dim", "2", "--gt-k", "128"]
            result = subprocess.run(command + list(extra), env=environment, capture_output=True,
                                    text=True, timeout=30)
            calls = trace.read_text().splitlines() if trace.exists() else []
            output = json.loads(result.stdout) if result.stdout.strip() else None
            return result, output, calls

    def test_exact_stride_and_independent_single_queries(self):
        result, output, calls = self.run_verifier()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output["gt_stride"], 128)
        self.assertEqual(output["tested_nq"], 4)
        self.assertEqual(output["query_mode"], "independent_nq_1_C_ABI_not_SQL")
        self.assertEqual(calls.count("search 1"), 8)
        self.assertEqual(calls.count("build 160"), 1)
        self.assertEqual(calls.count("free 0"), 1)
        for group in output["groups"]:
            self.assertEqual(group["recall"], 1.0)
            self.assertTrue(group["integrity_pass"])
            self.assertTrue(math.isfinite(group["qps"]))
        self.assertIn("not official score", output["label"])

    def test_rejects_incomplete_and_invalid_outputs(self):
        for mode in ("unwritten", "partial", "duplicate", "invalid", "nonfinite",
                     "negative", "wrong_distance", "unsorted"):
            with self.subTest(mode=mode):
                result, output, calls = self.run_verifier(mode)
                self.assertEqual(result.returncode, 1, (mode, result.stdout, result.stderr))
                self.assertTrue(any(not g["integrity_pass"] for g in output["groups"]))
                self.assertEqual(calls.count("free 0"), 1)

    def test_errors_do_not_report_success(self):
        for mode, message in (("search_error", "search failed"), ("null_build", "returned NULL")):
            with self.subTest(mode=mode):
                result, output, calls = self.run_verifier(mode)
                self.assertEqual(result.returncode, 1)
                self.assertIsNone(output)
                self.assertIn(message, result.stderr)
                if mode == "search_error":
                    self.assertIn("free 0", calls)

    def test_query_subset_is_explicit(self):
        result, output, calls = self.run_verifier(extra=("--limit", "2"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((output["input_nq"], output["tested_nq"]), (4, 2))
        self.assertEqual(calls.count("search 1"), 4)

    def test_nonfinite_inputs_rejected_before_build(self):
        for target in ("base", "queries"):
            original = self.base if target == "base" else self.queries
            for invalid in (math.nan, math.inf, -math.inf):
                with self.subTest(target=target, invalid=invalid):
                    values = array.array("f", original)
                    values[0] = invalid
                    path = self.root / (target + "-nonfinite.f32")
                    with path.open("wb") as stream:
                        values.tofile(stream)
                    result, output, calls = self.run_verifier(**{target: path})
                    self.assertEqual(result.returncode, 1)
                    self.assertIsNone(output)
                    self.assertIn("finite FP32", result.stderr)
                    self.assertFalse(calls)

    def test_invalid_ground_truth_rejected_before_build(self):
        for mode in ("short", "duplicate", "invalid"):
            with self.subTest(mode=mode):
                values = array.array("i", self.truth)
                if mode == "short":
                    values.pop()
                elif mode == "duplicate":
                    values[1] = values[0]
                else:
                    values[0] = 160
                path = self.root / (mode + "-gt.i32")
                with path.open("wb") as stream:
                    values.tofile(stream)
                result, output, calls = self.run_verifier(gt=path)
                self.assertEqual(result.returncode, 1)
                self.assertIsNone(output)
                self.assertFalse(calls)

    def test_snapshot_in_separate_process_without_build(self):
        with tempfile.TemporaryDirectory(prefix="snapshot-", dir=self.root) as directory:
            snapshot = Path(directory) / "index.bin"
            result, built, _ = self.run_verifier(extra=("--write-snapshot", str(snapshot)))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertGreater(built["snapshot"]["bytes"], 0)
            original = snapshot.read_bytes()
            result, loaded, calls = self.run_verifier("no_build", ("--load-snapshot", str(snapshot)))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(loaded["operation"], "deserialize_only_no_build")
            self.assertNotIn("build 160", calls)
            self.assertIn("deserialize 0", calls)
            self.assertEqual([g["first_ids"] for g in built["groups"]],
                             [g["first_ids"] for g in loaded["groups"]])
            result, _, calls = self.run_verifier(extra=("--write-snapshot", str(snapshot)))
            self.assertEqual(result.returncode, 1)
            self.assertEqual(snapshot.read_bytes(), original)
            self.assertNotIn("serialize 0", calls)

    def test_serialization_error_is_failure(self):
        with tempfile.TemporaryDirectory(prefix="snapshot-error-", dir=self.root) as directory:
            snapshot = Path(directory) / "index.bin"
            result, output, calls = self.run_verifier("serialize_error", ("--write-snapshot", str(snapshot)))
            self.assertEqual(result.returncode, 1)
            self.assertIsNone(output)
            self.assertIn("serialization failed", result.stderr)
            self.assertIn("free 0", calls)


if __name__ == "__main__":
    print("CPU mock ABI contract tests only; no GPU/SQL performance or official score.", flush=True)
    unittest.main(verbosity=2)
