import hashlib,json,statistics,subprocess,time,uuid
from pathlib import Path
import sys
sys.path.insert(0,'tests')
from benchmark import workload,source_digest
from client import Client
from cold_start_benchmark import check
batches,queries,truth=workload(20261114,50000,128,200)
clients={};volumes=[];names=[];report={'scope':'LOCAL direct Linux Docker CLI/socket interleaved warm-query control; no platform E','source_sha256':source_digest(),'rows':50000,'dimension':128,'queries_per_round':200,'memory_limit_bytes':128*1024*1024,'swap_max':0,'cpus':2,'seed':20261114,'policy':'Both DBs populated before timing, 20 warmups each, alternate version per individual query and rotate query order. Oracle checks outside each query timer. Raw milliseconds retained; no host cache eviction.','images':{},'rounds':[]}
try:
 for label in ['baseline','candidate']:
  name='mvdb-interleave-'+uuid.uuid4().hex[:10];vol=name+'-data';names.append(name);volumes.append(vol)
  subprocess.run(['docker','volume','create',vol],check=True,stdout=subprocess.DEVNULL)
  image='minivectordb:cold-start-'+label
  report['images'][label]=subprocess.check_output(['docker','image','inspect',image,'--format','{{.Id}}'],text=True).strip()
  c=Client(['docker','run','--rm','--platform','linux/amd64','--network','none','--name',name,'--cpus','2','--memory','128m','--memory-swap','128m','-i','-v',vol+':/data',image],'/data',timeout=240);clients[label]=c
  c.ok('CREATE TABLE t(id BIGINT PRIMARY KEY,g BIGINT,s VARCHAR(32),v VECTOR(128));')
  for sql in batches:assert c.ok(sql)['affected_rows']==100
 for label,c in clients.items():
  for sql,wanted in zip(queries[:20],truth[:20]):check(c.ok(sql)['rows'],wanted)
 for round_number in range(9):
  times={n:[] for n in clients}
  for j in range(len(queries)):
   i=(j+round_number*17)%len(queries)
   order=['baseline','candidate'] if (j+round_number)%2==0 else ['candidate','baseline']
   for label in order:
    start=time.perf_counter();actual=clients[label].ok(queries[i])['rows'];times[label].append((time.perf_counter()-start)*1000)
    check(actual,truth[i])
  item={'round':round_number,'milliseconds':times,'total_ms':{n:sum(t) for n,t in times.items()}}
  item['paired_speedup']=item['total_ms']['baseline']/item['total_ms']['candidate'];report['rounds'].append(item)
  report['median_paired_speedup']=statistics.median(r['paired_speedup'] for r in report['rounds'])
  Path('../docker-interleaved-warm.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({k:v for k,v in item.items() if k!='milliseconds'}),flush=True)
 report['exact_queries_checked']=20*2+9*200*2
 Path('../docker-interleaved-warm.json').write_text(json.dumps(report,indent=2)+'\n')
finally:
 for c in clients.values():
  if c.process.poll() is None:c.close()
 for name in names:subprocess.run(['docker','rm','-f',name],capture_output=True)
 for volume in volumes:subprocess.run(['docker','volume','rm',volume],capture_output=True)
