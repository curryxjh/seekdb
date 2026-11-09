#!/usr/bin/env python3
"""Foreground adapter used by container_suite.py; each test gets a Linux volume."""
import argparse
import hashlib
import os
import subprocess

p = argparse.ArgumentParser()
p.add_argument("--data", required=True)
args = p.parse_args()
prefix = os.environ["MVDB_VOLUME_PREFIX"]
volume = prefix + "-" + hashlib.sha256(os.path.abspath(args.data).encode()).hexdigest()[:20]
subprocess.run(["docker", "volume", "create", "--label", "mvdb-test-run=" + prefix, volume],
               check=True, stdout=subprocess.DEVNULL)
command = ["docker", "run", "--rm", "--platform", "linux/amd64", "--network", "none",
           "--memory", "128m", "--memory-swap", "128m", "--cpus", "2",
           "-i", "-v", volume + ":/data",
           os.environ.get("MVDB_IMAGE", "minivectordb:local")]
os.execvp(command[0], command)
