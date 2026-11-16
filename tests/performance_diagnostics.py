"""External paired benchmark; diagnostic records never use the stdout protocol.

Run from the repository root. See docs/performance-diagnostics.md for commands
and interpretation. No database process is spawned by the resource observer.
"""
import argparse
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid

from benchmark import source_digest, workload
from client import Client


OBSERVER_IMAGE = "busybox@sha256:a46257e06c82ad85242d0d38077d0a52d9896389ca60a1a59d3463db586e5fa4"
OBSERVER_SCRIPT = r"""
while read cid; do
  group=/cg/docker/$cid
  for field in memory.max memory.swap.max memory.peak memory.current memory.stat memory.events io.stat cpu.stat; do
    echo "@$field"
    cat "$group/$field"
  done
  echo "@processes"
  for pid in $(cat "$group/cgroup.procs"); do
    echo "pid $pid"
    cat "/proc/$pid/status" | sed -n '/^Name:/p; /^VmRSS:/p; /^VmHWM:/p; /^RssAnon:/p; /^RssFile:/p'
  done
  echo "@end"
done
"""


def output(*args):
    return subprocess.check_output(args, text=True).strip()


class Observer:
    def __init__(self):
        self.process = subprocess.Popen(
            ["docker", "run", "--rm", "--platform", "linux/arm64", "--network", "none",
             "--pid=host", "--cgroupns=host", "-v", "/sys/fs/cgroup:/cg:ro",
             "-i", "--entrypoint", "sh", OBSERVER_IMAGE, "-c", OBSERVER_SCRIPT],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)

    def snapshot(self, cid):
        self.process.stdin.write(cid + "\n")
        self.process.stdin.flush()
        result = {}
        field = None
        while True:
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError("resource observer exited")
            line = line.rstrip("\n")
            if line == "@end":
                break
            if line.startswith("@"):
                field = line[1:]
                result[field] = ""
            else:
                result[field] += line + "\n"
        assert int(result["memory.max"]) == 128 * 1024**2, result
        assert int(result["memory.swap.max"]) == 0, result
        assert "oom_kill 0" in result["memory.events"], result
        return result

    def close(self):
        self.process.stdin.close()
        assert self.process.wait(timeout=20) == 0


class DiagnosticClient(Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []
        self.errors = []
        self.reader = threading.Thread(target=self.drain, daemon=True)
        self.reader.start()

    def drain(self):
        for line in self.process.stderr:
            if line.startswith(b"@diag "):
                self.records.append(json.loads(line[6:]))
            else:
                self.errors.append(line.decode("utf-8", "replace"))

    def close(self):
        if self.process.poll() is None:
            self.sequence += 1
            self.send_request({"id": self.sequence, "op": "shutdown"})
            assert self.receive() == {"id": self.sequence, "ok": True}
            self.process.stdin.close()
        assert self.process.wait(timeout=self.timeout) == 0
        self.reader.join(timeout=10)
        assert not self.reader.is_alive() and not self.errors, self.errors
        assert not self.pending and not self.process.stdout.read()
        self.process.stdout.close()
        self.process.stderr.close()


def checked_queries(responses, truth):
    digest = hashlib.sha256()
    for i, (actual, expected) in enumerate(zip(responses, truth)):
        assert [r[0] for r in actual] == [key for dist, key in expected], i
        for (_, dist), (want, _) in zip(actual, expected):
            assert math.isclose(dist, want, rel_tol=1e-5, abs_tol=1e-5), (i, dist, want)
        digest.update(json.dumps(actual, separators=(",", ":")).encode())
    assert len(responses) == len(truth)
    return digest.hexdigest()


def run_item(image, label, seed, batches, queries, truth, observer, destination,
             mode, verify_restart, stride):
    name = "mvdb-diag-" + uuid.uuid4().hex[:12]
    volume = name + "-data"
    output("docker", "volume", "create", volume)
    command = ["docker", "run", "--rm", "--platform", "linux/amd64", "--network", "none",
               "--name", name, "--cpus", "2", "--memory", "128m", "--memory-swap", "128m",
               "-i", "-v", volume + ":/data", "-e", "MVDB_DIAG_MODE=" + mode,
               "-e", "MVDB_DIAG_SAMPLE_STRIDE=" + str(stride), image]
    client = None
    result = {"image": image, "label": label, "seed": seed, "mode": mode,
              "phases": {}, "requests": []}
    phases = result["phases"]
    try:
        start = time.perf_counter_ns()
        client = DiagnosticClient(command, "/data", timeout=180)
        result["container_start_to_ready_ms"] = (time.perf_counter_ns() - start) / 1e6
        cid = output("docker", "inspect", "--format", "{{.Id}}", name)
        phases["ready"] = observer.snapshot(cid)
        client.ok("CREATE TABLE t(id BIGINT PRIMARY KEY,g BIGINT,s VARCHAR(32),v VECTOR(128));")
        phases["before_write"] = observer.snapshot(cid)

        def request(sql, phase, filtered=None):
            start = time.perf_counter_ns()
            response = client.ok(sql)
            ns = time.perf_counter_ns() - start
            result["requests"].append({"id": client.sequence, "phase": phase,
                                       "filtered": filtered, "wall_ns": ns})
            return response

        for sql in batches:
            assert request(sql, "write")["affected_rows"] == 100
        phases["after_write"] = observer.snapshot(cid)
        first = [request(queries[i], "first", bool(i % 2))["rows"] for i in range(2)]
        assert checked_queries(first, truth[:2])
        phases["after_first"] = observer.snapshot(cid)
        for i in range(10):
            request(queries[i], "warmup", bool(i % 2))
        phases["before_steady"] = observer.snapshot(cid)
        responses = [request(sql, "steady", bool(i % 2))["rows"] for i, sql in enumerate(queries)]
        phases["after_steady"] = observer.snapshot(cid)
        result["response_sha256"] = checked_queries(responses, truth)
        # Probe transport scheduling floor; this is not subtracted from SQL costs.
        for i in range(30):
            request("SELECT id FROM t WHERE id=0;", "point_probe")
        client.close()
        result["diagnostic_records"] = client.records
        client = None
        if verify_restart:
            start = time.perf_counter_ns()
            client = DiagnosticClient(command, "/data", timeout=180)
            result["restart_to_ready_ms"] = (time.perf_counter_ns() - start) / 1e6
            restarted = request(queries[0], "restart_first")["rows"]
            assert checked_queries([restarted], truth[:1])
            scalar = client.ok("SELECT id,g,s FROM t ORDER BY id;")["rows"]
            assert scalar == [[i, i % 4, f"row-{i}"] for i in range(10000)]
            import re
            expected = [json.loads(x) for sql in batches
                        for x in re.findall(r"'(\[[^']+\])'", sql)]
            vectors = client.ok("SELECT v FROM t ORDER BY id;")["rows"]
            assert [row[0] for row in vectors] == expected
            result["restart_all_content_equal"] = True
            client.close()
            result["restart_diagnostic_records"] = client.records
            client = None
        with gzip.open(destination, "wt") as stream:
            json.dump(result, stream, separators=(",", ":"))
        print(json.dumps({"label": label, "seed": seed, "mode": mode,
                          "write_ms": sum(r["wall_ns"] for r in result["requests"] if r["phase"] == "write") / 1e6,
                          "query_ms": sum(r["wall_ns"] for r in result["requests"] if r["phase"] == "steady") / 1e6,
                          "records": len(result["diagnostic_records"])}), flush=True)
        return result
    finally:
        if client and client.process.poll() is None:
            client.close()
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        subprocess.run(["docker", "volume", "rm", volume], capture_output=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--image", default="minivectordb:nov16-number-clean")
    p.add_argument("--diagnostic-image")
    p.add_argument("--output", required=True)
    p.add_argument("--pairs", type=int, default=3)
    p.add_argument("--queries", type=int, default=1000)
    p.add_argument("--mode", default="events", choices=["coarse", "events", "shadow", "alloc"])
    p.add_argument("--seed", type=int, default=20261109)
    p.add_argument("--stride", type=int, default=64)
    p.add_argument("--skip-restart", action="store_true")
    args = p.parse_args()
    destination = Path(args.output)
    destination.mkdir(parents=True, exist_ok=False)
    batches, queries, truth = workload(args.seed, 10000, 128, args.queries)
    data_hash = hashlib.sha256("\n".join(batches + queries).encode()).hexdigest()
    images = list(dict.fromkeys([args.image, args.diagnostic_image or args.image]))
    identity = {"source_sha256": source_digest(), "workload_sha256": data_hash,
                "argv": os.sys.argv, "rows": 10000, "dimension": 128, "K": 10,
                "queries": args.queries, "seed": args.seed, "images": {},
                "limitations": [
                    "Apple Silicon emulates Linux amd64; no host/cache isolation.",
                    "External observer, no observer child in database cgroup.",
                    "io.stat is cumulative per device; phase increments are derived later.",
                    "memory.peak is cumulative; boundary VmRSS/VmHWM are separate.",
                    "First means empty process caches, not evicted host file cache.",
                    "Per-request wall includes Python JSON, Docker pipes and scheduling."]}
    for image in images:
        identity["images"][image] = {
            "image_id": output("docker", "image", "inspect", image, "--format", "{{.Id}}"),
            "binary_sha256": output("docker", "run", "--rm", "--platform", "linux/amd64",
                                    "--entrypoint", "sha256sum", image, "/usr/local/bin/minivectordb").split()[0]}
    (destination / "identity.json").write_text(json.dumps(identity, indent=2))
    observer = Observer()
    try:
        reference = None
        for pair in range(args.pairs):
            order = [("A", args.image), ("B", args.diagnostic_image or args.image)]
            if pair % 2:
                order.reverse()
            for label, image in order:
                result = run_item(image, f"{pair}-{label}", args.seed, batches, queries, truth,
                                  observer, destination / f"{pair}-{label}.json.gz", args.mode,
                                  not args.skip_restart, args.stride)
                if reference is None:
                    reference = result["response_sha256"]
                assert result["response_sha256"] == reference, "response bytes differ"
    finally:
        observer.close()


if __name__ == "__main__":
    main()
