"""Run unmodified public assertions and regressions against the production image."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import uuid


def run(image, output):
    prefix = "mvdb-suite-" + uuid.uuid4().hex[:12]
    env = {**os.environ, "MVDB_VOLUME_PREFIX": prefix, "MVDB_IMAGE": image}
    wrapper = str(Path(__file__).with_name("docker_db.py").resolve())
    if output:
        Path(output).mkdir(parents=True, exist_ok=True)
    try:
        for suite in ["public", "regression"]:
            command = [sys.executable, str(Path(__file__).with_name(suite + ".py")),
                       "--binary", wrapper]
            result = subprocess.run(command, env=env, capture_output=True, text=True)
            text = result.stdout + result.stderr
            print(text, end="", flush=True)
            if output:
                Path(output, "container-" + suite + ".txt").write_text(text)
            result.check_returncode()
    finally:
        volumes = subprocess.check_output(
            ["docker", "volume", "ls", "-q", "--filter", "label=mvdb-test-run=" + prefix],
            text=True).split()
        if volumes:
            subprocess.run(["docker", "volume", "rm", *volumes],
                           check=True, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--image", default="minivectordb:local")
    p.add_argument("--output")
    args = p.parse_args()
    run(args.image, args.output)
