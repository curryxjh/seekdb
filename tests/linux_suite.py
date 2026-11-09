"""Run fault-enabled Linux x86_64 tests in the pinned compiler image, offline."""
import argparse
from pathlib import Path
import subprocess
import uuid


def run(output):
    root = Path(__file__).resolve().parent.parent
    compiler = (root / "Dockerfile").read_text().splitlines()[0].split()[1]
    volume = "mvdb-linux-" + uuid.uuid4().hex[:12]
    subprocess.run(["docker", "volume", "create", volume], check=True,
                   stdout=subprocess.DEVNULL)
    script = """set -eu
g++ -std=c++20 -O3 -Wall -Wextra -Wpedantic -Wconversion -Wshadow \
    -DMINIVECTORDB_FAULTS minivectordb/*.cpp -o /data/minivectordb-fault
python3 tests/public.py --binary /data/minivectordb-fault --temp-root /data
python3 tests/regression.py --binary /data/minivectordb-fault --temp-root /data
python3 tests/storage_splits.py --binary /data/minivectordb-fault --temp-root /data
python3 tests/crash.py --binary /data/minivectordb-fault --temp-root /data
"""
    try:
        result = subprocess.run(
            ["docker", "run", "--rm", "--platform", "linux/amd64", "--network", "none",
             "--memory", "512m", "--memory-swap", "512m", "--cpus", "2",
             "-e", "TMPDIR=/data", "-v", str(root) + ":/src:ro", "-v", volume + ":/data",
             "-w", "/src", "--entrypoint", "sh", compiler, "-c", script],
            capture_output=True, text=True)
        text = result.stdout + result.stderr
        print(text, end="")
        if output:
            Path(output).write_text(text)
        result.check_returncode()
    finally:
        subprocess.run(["docker", "volume", "rm", volume], check=True,
                       stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--output")
    run(p.parse_args().output)
