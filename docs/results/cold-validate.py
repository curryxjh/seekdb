import hashlib,json,os,platform,subprocess,time
from pathlib import Path
import sys
sys.path.insert(0,'tests')
from benchmark import source_digest
commands=[]
def check(argv,env=None):
 before=time.monotonic()
 r=subprocess.run(argv,capture_output=True,text=True,env={**os.environ,**(env or {})})
 item={'argv':argv,'env':env or {},'exit_status':r.returncode,'elapsed_s':round(time.monotonic()-before,3),'stdout':r.stdout,'stderr':r.stderr}
 commands.append(item)
 Path('../native-validation.json').write_text(json.dumps({'scope':'LOCAL native Linux checks; no platform evaluation','source_sha256':source_digest(),'platform':platform.platform(),'commands':commands,'passed':all(x['exit_status']==0 for x in commands)},ensure_ascii=False,indent=2)+'\n')
 print(json.dumps(item),flush=True)
 if r.returncode:raise SystemExit(r.returncode)
for name in ['protocol','regression','query_cache','vector_numbers','vector_sweep','screening']:
 check(['python3','tests/'+name+'.py','--binary','build/minivectordb'])
check(['sh','build.sh'],{'SANITIZE':'1','FAULTS':'1','OUTPUT':'build/minivectordb-sanitize'})
for name in ['protocol','public','regression','query_cache','vector_numbers','vector_sweep','screening','crash','commit_log','large_transaction']:
 args=['python3','tests/'+name+'.py','--binary','build/minivectordb-sanitize']
 if name=='commit_log':args+=['--legacy-binary','../../perf5000/baseline/build/minivectordb']
 if name=='large_transaction':args+=['--fault-writeback']
 check(args,{'ASAN_OPTIONS':'detect_leaks=1','UBSAN_OPTIONS':'halt_on_error=1'})
for name in ['screen_quantize','screen_bounds','screen_stream']:
 for flags,tag in [(['-O3'],'release'),(['-O1','-g','-fsanitize=address,undefined','-fno-omit-frame-pointer'],'sanitize')]:
  out='build/'+name+'-'+tag
  argv=['g++','-std=c++20','-Wall','-Wextra','-Wpedantic','-Wconversion','-Wshadow',*flags,'tests/'+name+'.cpp']
  if name=='screen_bounds':argv+=['minivectordb/screen.cpp']
  check(argv+['-o',out])
  check([out],{'ASAN_OPTIONS':'detect_leaks=1','UBSAN_OPTIONS':'halt_on_error=1'})
