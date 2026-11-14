import argparse,csv,json,pathlib,time
p=argparse.ArgumentParser();p.add_argument('--op',choices=['clean','double','triple','merge','fail','oversize'],required=True);p.add_argument('--input',action='append',default=[]);p.add_argument('--output',required=True);p.add_argument('--audit',required=True);a=p.parse_args()
with open(a.audit,'a') as f:f.write(json.dumps({'op':a.op,'inputs':a.input,'output':a.output,'event':'start'})+'\n')
rows=[]
for name in a.input:
 with open(name,newline='') as f:rows.extend(list(csv.DictReader(f)))
time.sleep(.06 if a.op=='double' else .02)
output=pathlib.Path(a.output);output.parent.mkdir(parents=True,exist_ok=True)
if a.op=='fail':output.write_text('partial\n');raise SystemExit(7)
if a.op=='oversize':
 with output.open('w') as f:
  f.write('id,value\n')
  for i in range(800):f.write(f'{i},{i}\n');f.flush();time.sleep(.001)
else:
 with output.open('w',newline='') as f:
  w=csv.DictWriter(f,fieldnames=['id','value']);w.writeheader()
  for row in rows:
   if a.op=='double':row['value']=str(int(row['value'])*2)
   if a.op=='triple':row['value']=str(int(row['value'])*3)
   w.writerow(row)
with open(a.audit,'a') as f:f.write(json.dumps({'op':a.op,'event':'done'})+'\n')
