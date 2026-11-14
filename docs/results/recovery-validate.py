import hashlib,json,os,platform,subprocess,time
from pathlib import Path
import sys
sys.path.insert(0,"tests")
from benchmark import source_digest
results=[]
def check(argv,env=None):
 start=time.monotonic()
 r=subprocess.run(argv,capture_output=True,text=True,env={**os.environ,**(env or {})})
 item={"argv":argv,"env":env or {},"exit_status":r.returncode,"elapsed_s":round(time.monotonic()-start,3),"stdout":r.stdout,"stderr":r.stderr}
 results.append(item)
 Path("../validation-results.json").write_text(json.dumps({"scope":"LOCAL native Linux validation; no Docker image or platform evaluation", "source_sha256":source_digest(),"platform":platform.platform(),"commands":results,"passed":all(x["exit_status"]==0 for x in results)},ensure_ascii=False,indent=2)+"\n")
 print(json.dumps({k:v for k,v in item.items() if k not in ["stderr"]}),flush=True)
 if r.returncode:
  print(r.stderr,flush=True);raise SystemExit(r.returncode)
check(["python3","protocol_check/run.py","--command",json.dumps(["build/minivectordb","--data","{data_dir}"]),"--output","../public-validation"])
for name in ["protocol","public","regression","query_cache","vector_numbers","vector_sweep","screening","storage_splits"]:
 check(["python3","tests/"+name+".py","--binary","build/minivectordb"])
check(["sh","build.sh"],{"FAULTS":"1","OUTPUT":"build/minivectordb-fault"})
for name in ["crash","commit_log","large_transaction"]:
 argv=["python3","tests/"+name+".py","--binary","build/minivectordb-fault"]
 if name=="commit_log":argv += ["--legacy-binary","../../perf5000/baseline/build/minivectordb"]
 if name=="large_transaction":argv += ["--fault-writeback"]
 check(argv)
check(["sh","build.sh"],{"SANITIZE":"1","FAULTS":"1","OUTPUT":"build/minivectordb-sanitize"})
for name in ["public","protocol","regression","query_cache","vector_numbers","vector_sweep","screening","storage_splits","crash","commit_log"]:
 argv=["python3","tests/"+name+".py","--binary","build/minivectordb-sanitize"]
 if name=="commit_log":argv += ["--legacy-binary","../../perf5000/baseline/build/minivectordb"]
 check(argv,{"ASAN_OPTIONS":"detect_leaks=1","UBSAN_OPTIONS":"halt_on_error=1"})
check(["g++","-std=c++20","-O1","-g","-fsanitize=address,undefined","-fno-omit-frame-pointer","tests/screen_bounds.cpp","minivectordb/screen.cpp","-o","build/screen-bounds-sanitize"])
check(["build/screen-bounds-sanitize"],{"ASAN_OPTIONS":"detect_leaks=1","UBSAN_OPTIONS":"halt_on_error=1"})
check(["python3","tests/native_resource.py","--binary","build/minivectordb","--output","../native-resource-96m.json"])
check(["g++","-std=c++20","-O1","-g","-fsanitize=address,undefined","-fno-omit-frame-pointer","tests/screen_stream.cpp","-o","build/screen-stream-sanitize"])
check(["build/screen-stream-sanitize"],{"ASAN_OPTIONS":"detect_leaks=1","UBSAN_OPTIONS":"halt_on_error=1"})
Path("build/limited-cache").write_bytes(b'#!/bin/sh\nexec systemd-run --user --quiet --pipe --wait --collect --service-type=exec -p MemoryMax=32M -p MemorySwapMax=0 "$(dirname "$0")/minivectordb" "$@"\n')
Path("build/limited-cache").chmod(0o755)
check(["python3","tests/query_cache.py","--binary","build/limited-cache"])
print("PASS complete native validation",flush=True)
