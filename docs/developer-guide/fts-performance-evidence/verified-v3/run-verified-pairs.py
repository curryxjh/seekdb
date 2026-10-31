from pathlib import Path
import os, subprocess

root = Path('/home/beaten/fts-results/v3')
root.mkdir(exist_ok=True)
versions = {
    'original': '/home/beaten/fts-results/seekdb-original-exact',
    'candidate': '/home/beaten/fts-results/v3/seekdb-candidate',
}
for i, variant in enumerate(['original', 'candidate', 'candidate', 'original'], 1):
    label = f'verified-{i}-{variant}'
    assert not (root/f'report_{label}.txt').exists(), label
    print('START', label, flush=True)
    subprocess.run(['python3', '/mnt/d/ob大赛/fts-opt/verified-run.py',
                    label, versions[variant]], env=dict(os.environ, EVIDENCE_DIR=str(root)), check=True)
    print('COMPLETE', label, flush=True)
