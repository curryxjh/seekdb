#!/usr/bin/env python3
"""Development-only verifier; not the contest's SQL scorer.

Loads the exact shared-library path (no link-time SONAME ambiguity), executes
independent nq=1 calls, and reads the GT row stride independently of Top-k.
No query tiling, dataset-specific answers, or changes to the supplied data.
A synthetic input remains a proxy; only the official evaluator awards a score.

Example:
  python3 -I verify_bridge.py --library /tmp/candidate/libseekdb_cuvs_bridge.so \
    --base base.f32 --queries query.f32 --gt gt.i32 --dim 128 --gt-k 100
Add --write-snapshot PATH, then run a separate process with --load-snapshot PATH
to exercise deserialize/search without invoking the builder in that process.
"""
import argparse
import array
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time


def read_array(path, code):
    data = array.array(code)
    if data.itemsize != 4 or sys.byteorder != "little":
        raise ValueError("requires little-endian 32-bit floats/IDs")
    size = path.stat().st_size
    if size % 4:
        raise ValueError(f"unaligned input: {path}")
    with path.open("rb") as stream:
        data.fromfile(stream, size // 4)
    return data


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", required=True, type=Path)
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--queries", required=True, type=Path)
    parser.add_argument("--gt", required=True, type=Path)
    parser.add_argument("--dim", required=True, type=int)
    parser.add_argument("--gt-k", required=True, type=int)
    parser.add_argument("--label", default="development proxy, not official score")
    parser.add_argument("--limit", type=int, help="explicit query subset; never presented as full nq")
    snapshots = parser.add_mutually_exclusive_group()
    snapshots.add_argument("--write-snapshot", type=Path)
    snapshots.add_argument("--load-snapshot", type=Path)
    args = parser.parse_args()
    if args.dim < 1 or args.gt_k < 100 or (args.limit is not None and args.limit < 1):
        raise ValueError("dim and limit must be positive; GT must contain at least 100 IDs per row")
    library_path = args.library.resolve(strict=True)
    base = read_array(args.base, "f")
    queries = read_array(args.queries, "f")
    truth = read_array(args.gt, "i")
    if not base or not queries or len(base) % args.dim or len(queries) % args.dim:
        raise ValueError("base/query dimensions do not match supplied dim")
    if any(not math.isfinite(x) for x in base) or any(not math.isfinite(x) for x in queries):
        raise ValueError("base/query vectors must contain only finite FP32 values")
    n = len(base) // args.dim
    input_nq = len(queries) // args.dim
    if n < 100 or len(truth) != input_nq * args.gt_k:
        raise ValueError("need n >= 100 and exact query_count * gt_k ground-truth IDs")
    nq = min(input_nq, args.limit) if args.limit is not None else input_nq
    for qi in range(nq):
        row = truth[qi * args.gt_k:qi * args.gt_k + args.gt_k]
        if len(set(row)) != args.gt_k or any(x < 0 or x >= n for x in row):
            raise ValueError(f"invalid or duplicate GT IDs in query {qi}")
    FloatPtr = ctypes.POINTER(ctypes.c_float)
    UIntPtr = ctypes.POINTER(ctypes.c_uint32)
    lib = ctypes.CDLL(str(library_path), mode=os.RTLD_LOCAL)
    lib.seekdb_cuvs_build.argtypes = [FloatPtr, ctypes.c_long, ctypes.c_long]
    lib.seekdb_cuvs_build.restype = ctypes.c_void_p
    lib.seekdb_cuvs_search.argtypes = [ctypes.c_void_p, FloatPtr, ctypes.c_long, ctypes.c_long, UIntPtr, FloatPtr]
    lib.seekdb_cuvs_search.restype = ctypes.c_int
    lib.seekdb_cuvs_free.argtypes = [ctypes.c_void_p]
    lib.seekdb_cuvs_free.restype = None
    lib.seekdb_cuvs_serialize.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    lib.seekdb_cuvs_serialize.restype = ctypes.c_int
    lib.seekdb_cuvs_deserialize.argtypes = [ctypes.c_char_p, ctypes.c_long, ctypes.c_long]
    lib.seekdb_cuvs_deserialize.restype = ctypes.c_void_p
    base_view = (ctypes.c_float * len(base)).from_buffer(base)
    query_view = (ctypes.c_float * len(queries)).from_buffer(queries)
    start = time.perf_counter()
    if args.load_snapshot:
        snapshot = args.load_snapshot.resolve(strict=True)
        handle = lib.seekdb_cuvs_deserialize(os.fsencode(snapshot), n, args.dim)
        operation = "deserialize_only_no_build"
    else:
        handle = lib.seekdb_cuvs_build(base_view, n, args.dim)
        operation = "build"
    setup_s = time.perf_counter() - start
    if not handle:
        raise RuntimeError(f"{operation} returned NULL")
    output = {"label": args.label, "library": str(library_path),
              "library_sha256": digest(library_path),
              "base_sha256": digest(args.base), "queries_sha256": digest(args.queries),
              "gt_sha256": digest(args.gt), "n": n, "dim": args.dim,
              "input_nq": input_nq, "tested_nq": nq, "gt_stride": args.gt_k,
              "query_mode": "independent_nq_1_C_ABI_not_SQL",
              "operation": operation, "setup_s": setup_s,
              "environment": {k: v for k, v in os.environ.items() if k.startswith("SEEKDB_CUVS_")},
              "groups": []}
    try:
        # No warmup: the first fixed query is measured separately for build+first.
        for k in (10, 100):
            ids = (ctypes.c_uint32 * k)()
            distances = (ctypes.c_float * k)()
            hits = 0
            latencies = []
            first_ids = None
            integrity = {"invalid_ids": 0, "duplicate_ids": 0,
                         "nonfinite_distances": 0, "negative_distances": 0,
                         "unsorted_rows": 0, "distance_mismatches": 0}
            for qi in range(nq):
                # A successful call must write every output, not reuse the last
                # query's results or leave zero-initialized buffers untouched.
                for j in range(k):
                    ids[j] = 0xFFFFFFFF
                    distances[j] = math.nan
                query = ctypes.cast(ctypes.byref(query_view, qi * args.dim * 4), FloatPtr)
                start = time.perf_counter()
                rc = lib.seekdb_cuvs_search(handle, query, 1, k, ids, distances)
                latencies.append(time.perf_counter() - start)
                if rc:
                    raise RuntimeError(f"search failed: k={k} query={qi} rc={rc}")
                row_ids = list(ids)
                row_dist = list(distances)
                if first_ids is None:
                    first_ids = row_ids
                integrity["invalid_ids"] += sum(i >= n for i in row_ids)
                integrity["duplicate_ids"] += k - len(set(row_ids))
                integrity["nonfinite_distances"] += sum(not math.isfinite(d) for d in row_dist)
                integrity["negative_distances"] += sum(d < -0.01 for d in row_dist)
                integrity["unsorted_rows"] += int(any(row_dist[j] > row_dist[j+1] + 0.001 for j in range(k-1)))
                gt = set(truth[qi * args.gt_k:qi * args.gt_k + k])
                hits += len(set(row_ids) & gt)
                # Validate actual ID/distance correspondence outside the timed search.
                if qi < 3:
                    q_offset = qi * args.dim
                    for idx, reported in zip(row_ids, row_dist):
                        if idx >= n or not math.isfinite(reported):
                            continue
                        offset = idx * args.dim
                        exact = sum((float(queries[q_offset+d])-float(base[offset+d]))**2 for d in range(args.dim))
                        if abs(reported-exact) > 0.005 * max(1.0, exact):
                            integrity["distance_mismatches"] += 1
            total = sum(latencies)
            ordered = sorted(latencies)
            output["groups"].append({"k": k, "recall": hits / (nq*k),
                "search_s": total, "qps": nq / total,
                "p95_ms": ordered[max(0, math.ceil(nq*0.95)-1)] * 1000,
                "first_query_s": latencies[0], "first_ids": first_ids,
                "integrity": integrity, "integrity_pass": not any(integrity.values())})
        output["setup_plus_first_query_s"] = setup_s + output["groups"][0]["first_query_s"]
        if args.write_snapshot:
            # Use an exclusive reservation; never overwrite an existing snapshot.
            snapshot = args.write_snapshot.absolute()
            fd = os.open(snapshot, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            start = time.perf_counter()
            rc = lib.seekdb_cuvs_serialize(handle, os.fsencode(snapshot))
            if rc:
                raise RuntimeError(f"snapshot serialization failed rc={rc}")
            output["snapshot"] = {"path": str(snapshot), "bytes": snapshot.stat().st_size,
                                  "sha256": digest(snapshot), "serialize_s": time.perf_counter()-start}
    finally:
        lib.seekdb_cuvs_free(handle)
    print(json.dumps(output, sort_keys=True), flush=True)
    return 0 if all(g["integrity_pass"] for g in output["groups"]) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"VERIFY_FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
