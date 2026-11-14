import hashlib,io,json,os,platform,subprocess,tarfile,time,uuid
from pathlib import Path
import sys
sys.path.insert(0,'tests')
from benchmark import source_digest
commands=[]
image='minivectordb:cold-start-candidate'
def check(argv):
 before=time.monotonic();r=subprocess.run(argv,capture_output=True,text=True)
 item={'argv':argv,'exit_status':r.returncode,'elapsed_s':round(time.monotonic()-before,3),'stdout':r.stdout,'stderr':r.stderr}
 commands.append(item)
 Path('../docker-validation.json').write_text(json.dumps({'scope':'LOCAL production Docker image checks; no platform evaluation','source_sha256':source_digest(),'driver_platform':platform.platform(),'commands':commands,'passed':all(x['exit_status']==0 for x in commands)},ensure_ascii=False,indent=2)+'\n')
 print(json.dumps(item),flush=True)
 if r.returncode:raise SystemExit(r.returncode)
check(['docker','run','--rm','--network','none','--entrypoint','sha256sum',image,'/usr/local/bin/minivectordb'])
check(['python3','protocol_check/run.py','--image',image,'--output','../docker-public'])
check(['python3','tests/protocol.py','--image',image])
check(['python3','tests/container_suite.py','--image',image,'--output','../docker-suites'])
check(['python3','tests/container_checks.py','--image',image,'--rows','40000','--output','../docker-resource-96m.json'])
name='mvdb-cold-kernels-'+uuid.uuid4().hex[:12]
try:
 check(['docker','run','--rm','-d','--platform','linux/amd64','--network','none','--name',name,'--entrypoint','sleep','gcc:14.2.0-bookworm','600'])
 check(['docker','exec',name,'g++','--version'])
 check(['docker','exec',name,'mkdir','/src'])
 buf=io.BytesIO()
 with tarfile.open(fileobj=buf,mode='w') as tar:
  tar.add('minivectordb',arcname='minivectordb')
  for kind in ['screen_quantize','screen_bounds','screen_stream']:tar.add('tests/'+kind+'.cpp',arcname='tests/'+kind+'.cpp')
 subprocess.run(['docker','exec','-i',name,'tar','-x','-C','/src'],input=buf.getvalue(),check=True)
 for kind in ['screen_quantize','screen_bounds','screen_stream']:
  argv=['docker','exec','-w','/src',name,'g++','-std=c++20','-O3','-Wall','-Wextra','-Wpedantic','-Wconversion','-Wshadow','tests/'+kind+'.cpp']
  if kind=='screen_bounds':argv+=['minivectordb/screen.cpp']
  check(argv+['-o','/tmp/'+kind]);check(['docker','exec',name,'/tmp/'+kind])
finally:subprocess.run(['docker','rm','-f',name],capture_output=True)
