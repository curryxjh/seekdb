"""Invoke the unmodified official protocol driver against this implementation."""
import json
from pathlib import Path
import subprocess
import sys
import time

root = Path(__file__).resolve().parents[1]
output = root / 'protocol-results' / ('local-' + str(time.time_ns()))
raise SystemExit(subprocess.call([sys.executable, str(root / 'protocol_check/run.py'),
    '--command', json.dumps([sys.executable, '-m', 'minivectordb', '--data-dir', '{data_dir}']),
    '--output', str(output), *sys.argv[1:]], cwd=root))
