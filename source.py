import argparse,json,time
p=argparse.ArgumentParser();p.add_argument('path');p.add_argument('--start',type=int,default=0);p.add_argument('--delay-scale',type=float,default=1);a=p.parse_args()
with open(a.path,encoding='utf-8') as f:
 for i,line in enumerate(f):
  if i<a.start:continue
  obj=json.loads(line);time.sleep(obj.pop('_delay_ms',0)*a.delay_scale/1000);print(json.dumps(obj,ensure_ascii=False),flush=True)
