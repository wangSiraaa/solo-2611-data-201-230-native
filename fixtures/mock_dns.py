#!/usr/bin/env python3
"""Frozen local authoritative transfer responder; not a sync client."""
import argparse,json,socket,struct
from pathlib import Path
import dns.message,dns.rrset,dns.rdatatype,dns.flags,dns.rcode
p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=11535);p.add_argument('--stage',choices=['base','two','wrap-base','wrap','fallback','bad-gap','truncated','unchanged'],default='base');p.add_argument('--log',default='dns-server.jsonl');a=p.parse_args()
root=Path(__file__).resolve().parent;data=json.loads((root/'stages.json').read_text());zone=data['zone']
def rr(s):
 owner,ttl,typ,rdata=s.split(' ',3);return dns.rrset.from_text(owner,int(ttl),'IN',typ,rdata)
def soa(serial):return rr(f'{zone} 60 SOA ns.{zone} hostmaster.{zone} {serial} 3600 600 86400 60')
def exact(c,n):
 b=b''
 while len(b)<n:
  x=c.recv(n-len(b))
  if not x:raise EOFError()
  b+=x
 return b
def log(**v):
 with open(a.log,'a')as f:f.write(json.dumps(v)+'\n')
with socket.socket()as s:
 s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('127.0.0.1',a.port));s.listen();print(json.dumps({'ready':True,'port':s.getsockname()[1],'stage':a.stage}),flush=True)
 while True:
  c,_=s.accept();c.settimeout(20)
  try:
   n=struct.unpack('!H',exact(c,2))[0];q=dns.message.from_wire(exact(c,n));typ=q.question[0].rdtype;serial=int(q.authority[0][0].serial)if q.authority else None;log(qtype=dns.rdatatype.to_text(typ),serial=serial,stage=a.stage)
   stage=data['stages'][a.stage];new=stage['serial'];records=stage['records']
   if str(q.question[0].name).lower()!=zone or typ not in (dns.rdatatype.AXFR,dns.rdatatype.IXFR):
    response=dns.message.make_response(q);response.set_rcode(dns.rcode.REFUSED);raw=response.to_wire();c.sendall(struct.pack('!H',len(raw))+raw);continue
   if typ==dns.rdatatype.AXFR or stage.get('fallback'):answers=[soa(new)]+[rr(x)for x in records]+[soa(new)]
   elif serial==new:answers=[soa(new)]
   elif 'deltas'in stage:
    answers=[soa(new)]
    for delta in stage['deltas']:answers += [soa(delta['old'])]+[rr(x)for x in delta['delete']]+[soa(delta['new'])]+[rr(x)for x in delta['add']]
    answers.append(soa(new))
   else:answers=[soa(new)]+[rr(x)for x in records]+[soa(new)]
   for i in range(0,len(answers),2):
    response=dns.message.make_response(q);response.flags|=dns.flags.AA;response.answer=answers[i:i+2];raw=response.to_wire();wire=struct.pack('!H',len(raw))+raw
    if stage.get('truncate')and i+2>=len(answers):c.sendall(wire[:len(wire)//2]);log(truncated=True);break
    c.sendall(wire)
  except (OSError,EOFError,ValueError)as e:log(error=type(e).__name__)
  finally:c.close()
