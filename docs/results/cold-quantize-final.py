import hashlib,io,json,os,subprocess,tarfile,time,uuid
from pathlib import Path
import sys
sys.path.insert(0,'tests')
from benchmark import source_digest
commands=[]
def check(argv,env=None):
 begin=time.monotonic();r=subprocess.run(argv,capture_output=True,text=True,env={**os.environ,**(env or {})})
 commands.append({'argv':argv,'env':env or {},'exit_status':r.returncode,'elapsed_s':time.monotonic()-begin,'stdout':r.stdout,'stderr':r.stderr})
 Path('../quantize-final.json').write_text(json.dumps({'scope':'LOCAL final test includes explicitly representable double immediately below half','source_sha256':source_digest(),'test_sha256':hashlib.sha256(Path('tests/screen_quantize.cpp').read_bytes()).hexdigest(),'commands':commands,'passed':all(c['exit_status']==0 for c in commands)},indent=2)+'\n')
 print(json.dumps(commands[-1]),flush=True);r.check_returncode()
for flags,tag in [(['-O3'],'release'),(['-O1','-g','-fsanitize=address,undefined','-fno-omit-frame-pointer'],'sanitize')]:
 out='build/screen-quantize-final-'+tag
 check(['g++','-std=c++20','-Wall','-Wextra','-Wpedantic','-Wconversion','-Wshadow',*flags,'tests/screen_quantize.cpp','-o',out])
 check([out],{'ASAN_OPTIONS':'detect_leaks=1','UBSAN_OPTIONS':'halt_on_error=1'})
name='mvdb-cold-final-quant-'+uuid.uuid4().hex[:10]
try:
 check(['docker','run','--rm','-d','--platform','linux/amd64','--network','none','--name',name,'--entrypoint','sleep','gcc:14.2.0-bookworm','300'])
 check(['docker','exec',name,'mkdir','/src'])
 buf=io.BytesIO()
 with tarfile.open(fileobj=buf,mode='w') as tar:
  for file in ['minivectordb/screen.cpp','minivectordb/screen.h','tests/screen_quantize.cpp']:tar.add(file,arcname=file)
 subprocess.run(['docker','exec','-i',name,'tar','-x','-C','/src'],input=buf.getvalue(),check=True)
 check(['docker','exec','-w','/src',name,'g++','-std=c++20','-O3','-Wall','-Wextra','-Wpedantic','-Wconversion','-Wshadow','tests/screen_quantize.cpp','-o','/tmp/quantize-final'])
 check(['docker','exec',name,'/tmp/quantize-final'])
finally:subprocess.run(['docker','rm','-f',name],capture_output=True)
