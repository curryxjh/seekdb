"""Diagnostic-only local run against a chosen MiniVectorDB executable."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from benchmark import workload, source_digest
from container_checks import metrics


def binary_digest(binary):
    if binary.startswith("docker:"):
        image = binary[7:]
        image_id = subprocess.check_output(
            ["docker", "image", "inspect", image, "--format", "{{.Id}}"], text=True).strip()
        digest = subprocess.check_output(
            ["docker", "run", "--rm", "--platform", "linux/amd64", "--network", "none",
             "--entrypoint", "/usr/bin/sha256sum", image, "/usr/local/bin/minivectordb"],
            text=True).split()[0]
        return {"image": image, "image_id": image_id, "binary_sha256": digest}
    return {"binary_sha256": hashlib.sha256(Path(binary).read_bytes()).hexdigest()}


class Process:
    def __init__(self, binary, directory, diag_path, volume=None):
        env = dict(os.environ)
        if binary.startswith("docker:"):
            diag_path = Path(diag_path).resolve()
            directory = Path(directory).resolve()
            env.pop("MVDB_DIAGNOSTICS_FILE", None)
            self.name = "mvdb-p0-diag-" + uuid.uuid4().hex[:12]
            command = ["docker", "run", "--rm", "--platform", "linux/amd64", "--name", self.name,
                       "--network", "none", "--cpus", "2", "--memory", "128m",
                       "--memory-swap", "128m", "-i", "-v", f"{volume or directory}:/data",
                       binary[7:], "--data", "/data"]
        else:
            env["MVDB_DIAGNOSTICS_FILE"] = str(diag_path)
            command = [binary, "--data", str(directory)]
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, env=env)
        self.seq = 0
        self.diag_path = Path(diag_path)
        assert self.read() == {"type": "ready"}

    def read(self):
        line = self.process.stdout.readline()
        assert line, self.process.poll()
        return json.loads(line)

    def request(self, sql=None, op="execute"):
        self.seq += 1
        body = {"id": self.seq, "op": op}
        if sql is not None:
            body.update(sql=sql, search_mode="exact")
        data = memoryview(json.dumps(body, separators=(",", ":")).encode() + b"\n")
        while data:
            written = self.process.stdin.write(data)
            assert written and written > 0
            data = data[written:]
        result = self.read()
        assert result.get("id") == self.seq and result.get("ok") is True, result
        return result

    def close(self):
        self.request(op="shutdown")
        self.process.stdin.close()
        self.process.wait(timeout=120)
        err = self.process.stderr.read().decode()
        assert self.process.returncode == 0, (self.process.returncode, err)
        if self.diag_path.exists():
            return self.diag_path.read_text()
        lines = [line for line in err.splitlines() if line.startswith("MVDB_")]
        assert len(lines) == 2, err
        return "\n".join(lines) + "\n"


def run(binary, output, rows, dim, query_count):
    volume = "mvdb-p0-diag-data-" + uuid.uuid4().hex[:12] if binary.startswith("docker:") else None
    if volume:
        subprocess.check_call(["docker", "volume", "create", volume], stdout=subprocess.DEVNULL)
    try:
        return measure(binary, output, rows, dim, query_count, volume)
    finally:
        if volume:
            subprocess.run(["docker", "volume", "rm", volume], capture_output=True)


def measure(binary, output, rows, dim, query_count, volume):
    batches, queries, truth = workload(20261116, rows, dim, query_count)
    with tempfile.TemporaryDirectory(prefix="mvdb-diag-") as tmp:
        data = Path(tmp) / "data"
        started = time.perf_counter()
        writer = Process(binary, data, Path(tmp) / "write.diag", volume)
        writer_startup_ms = (time.perf_counter() - started) * 1000
        writer.request(f"CREATE TABLE t(id BIGINT PRIMARY KEY,g BIGINT,s VARCHAR(32),v VECTOR({dim}));")
        t0 = time.perf_counter()
        for sql in batches:
            assert writer.request(sql)["affected_rows"] == 100
        write_wall_ms = (time.perf_counter() - t0) * 1000
        write_metrics = metrics(writer.name) if volume else None
        write_diagnostics = writer.close()

        started = time.perf_counter()
        reader = Process(binary, data, Path(tmp) / "query.diag", volume)
        reader_startup_ms = (time.perf_counter() - started) * 1000
        cold_started = time.perf_counter()
        first = reader.request(queries[0])["rows"]
        first_query_ms = (time.perf_counter() - cold_started) * 1000
        assert [r[0] for r in first] == [key for _, key in truth[0]]
        latencies = [first_query_ms]
        outputs = [first]
        for sql in queries[1:]:
            started = time.perf_counter()
            outputs.append(reader.request(sql)["rows"])
            latencies.append((time.perf_counter() - started) * 1000)
        for got, expected in zip(outputs, truth):
            assert [r[0] for r in got] == [key for _, key in expected]
        query_metrics = metrics(reader.name) if volume else None
        query_diagnostics = reader.close()
        query_wall_ms = sum(latencies)
        report = {
            "scope": ("diagnostic build; Apple Silicon emulating Linux amd64; named data volume; "
                      "counters perturb timings; not official score") if binary.startswith("docker:") else
                     "diagnostic build; native architecture; counters perturb timings; not official score",
            **binary_digest(binary),
            "source_sha256": source_digest(), "rows": rows, "dimensions": dim,
            "queries": query_count, "query_correctness": "exact IDs/order checked against independent oracle",
            "write_wall_ms": write_wall_ms, "query_wall_ms_sum": query_wall_ms,
            "first_query_wall_ms": first_query_ms,
            "warm_query_wall_ms_median": sorted(latencies[1:])[len(latencies[1:]) // 2],
            "filtered_warm_median_ms": statistics.median(latencies[1::2]),
            "unfiltered_warm_median_ms": statistics.median(latencies[2::2]),
            "query_latencies_ms": latencies,
            "memory_measurement": "cgroup cumulative memory.peak collected; process RSS not collected",
            "writer_startup_ms": writer_startup_ms, "reader_startup_ms": reader_startup_ms,
            "write_cgroup": write_metrics, "query_cgroup": query_metrics,
            "cgroup_caveat": "cumulative peak and I/O include startup; device attribution unverified",
            "write_diagnostics": write_diagnostics,
            "query_diagnostics": query_diagnostics,
        }
    Path(output).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if "diagnostics" not in k}, indent=2))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--binary", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--rows", type=int, default=10000)
    p.add_argument("--dimension", type=int, default=128)
    p.add_argument("--queries", type=int, default=1000)
    args = p.parse_args()
    run(args.binary, args.output, args.rows, args.dimension, args.queries)
