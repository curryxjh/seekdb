from pathlib import Path
import hashlib, json, os, re, subprocess, sys, threading, time

base = Path(os.environ.get('EVIDENCE_DIR', '/home/beaten/fts-results/v2'))
base.mkdir(exist_ok=True)
runtime = Path('/home/beaten/fts-runtime-v2')
label, binary = sys.argv[1:3]
binary = Path(binary).resolve(strict=True)
env = dict(os.environ, BIN=str(binary), BASE_DIR=str(runtime), OUT_DIR=str(base))
subprocess.run(['bash', '/home/beaten/fts-opt/start-seekdb.sh'], env=env, check=True)
pid = int((runtime/'run/seekdb.pid').read_text())
running = Path(f'/proc/{pid}/exe').resolve(strict=True)
assert running == binary, (running, binary)
identity = dict(label=label, pid=pid, executable=str(running),
                binary_sha256=hashlib.file_digest(binary.open('rb'), 'sha256').hexdigest())
(base/f'identity-{label}.json').write_text(json.dumps(identity, indent=2)+'\n')
print('VERIFIED', json.dumps(identity), flush=True)
if len(sys.argv)>3 and sys.argv[3]=='start':
    sys.exit(0)
steps=[]; done=threading.Event()
def monitor():
    previous=time.time()-time.monotonic()
    while not done.wait(.05):
        current=time.time()-time.monotonic()
        if abs(current-previous)>.02: steps.append(current-previous)
        previous=current
t=threading.Thread(target=monitor); t.start()
try:
    subprocess.run(['bash','/home/beaten/fts-opt/bench.sh',label,'0'],env=env,check=True)
finally:
    done.set(); t.join()
    (base/f'clock-{label}.json').write_text(json.dumps(steps)+'\n')
values=[float(x) for x in re.findall(r'total_ms=([-\d.]+)',(base/f'bench_{label}.log').read_text())]
assert len(values)==21 and min(values)>0 and not steps, (values,steps)
assert Path(f'/proc/{pid}/exe').resolve(strict=True)==binary
print('PASS: binary identity and all sample clocks verified', flush=True)
