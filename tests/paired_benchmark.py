"""Archived adjacent-pair runner, originally executed as build/paired_perf.py.
Uses the attested protocol-delivery baseline and current perf-final image.
"""
import sys,json,subprocess
from pathlib import Path
from benchmark import workload,item,source_digest
from compare import compare
base=json.loads(Path('docs/results/perf-5300-baseline.json').read_text());base['rounds']=[]
cand={**base,'source_sha256':source_digest(),'image_id':subprocess.check_output(['docker','image','inspect','minivectordb:perf-final','--format','{{.Id}}'],text=True).strip(),'rounds':[]}
base['timing_note']=cand['timing_note']='Adjacent baseline/candidate pairs; no agent builds/tests alongside; external host workloads and emulation still affect wall time'
for n,seed in enumerate([20261109,7,39034843]):
 b,q,t=workload(seed,10000,128,1000)
 for image,r in ([('minivectordb:protocol-delivery',base),('minivectordb:perf-final',cand)] if n%2==0 else [('minivectordb:perf-final',cand),('minivectordb:protocol-delivery',base)]):
  out={'seed':seed,'write':item(image,'write',b,q,t,10000,128,False),'query':item(image,'query',b,q,t,10000,128,False)}
  r['rounds'].append(out);print(json.dumps({'image':image,**out}),flush=True)
  path='docs/results/perf-paired-'+('baseline' if r is base else 'candidate')+'.json';Path(path).write_text(json.dumps(r,indent=2)+'\n')
result=compare(base,cand);Path('docs/results/perf-paired-comparison.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result['metric_medians_for_description_only']),flush=True)
