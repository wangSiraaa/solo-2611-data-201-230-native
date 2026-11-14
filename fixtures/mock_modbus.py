#!/usr/bin/env python3
"""Synthetic holding-register device, never writes a physical device."""
import argparse,json,socket,struct
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=11502);p.add_argument('--mode',default='normal');p.add_argument('--log',default='modbus-server.jsonl');a=p.parse_args();cfg=json.loads((Path(__file__).parent/'scenarios.json').read_text());regs={int(k):v for k,v in cfg['registers'].items()};changed=False;data_reads=0
if a.mode not in cfg['modes']:p.error('unknown mode')
if a.mode=='nonfinite':regs[4]=0;regs[5]=0x7fc0
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
 s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('127.0.0.1',a.port));s.listen();print(json.dumps({'ready':True,'port':s.getsockname()[1]}),flush=True)
 while True:
  c,_=s.accept();c.settimeout(20)
  try:
   while True:
    h=exact(c,7);tid,proto,length,unit=struct.unpack('!HHHB',h)
    if length<2 or length>254:raise ValueError('length')
    pdu=exact(c,length-1)
    if proto or unit!=1 or len(pdu)!=5 or pdu[0]!=3:reply=bytes([pdu[0]|128,1])
    else:
     start,count=struct.unpack('!HH',pdu[1:]);log(transaction_id=tid,start=start,count=count,version=regs[100]);data=start!=100
     if count<1 or count>4 or any(x not in regs for x in range(start,start+count))or(a.mode=='exception'and start<=8<start+count):reply=b'\x83\x02'
     else:reply=bytes([3,count*2])+b''.join(struct.pack('!H',regs[x])for x in range(start,start+count))
     if data:
      data_reads+=1
      if a.mode=='changing'or(a.mode=='change-once'and not changed):regs[100]=(regs[100]+2)&0xffff;regs[2]+=1;changed=True
      if a.mode=='wrong-tid'and data_reads==1:tid=(tid+1)&0xffff
    c.sendall(struct.pack('!HHHB',tid,0,len(reply)+1,unit)+reply)
  except (OSError,EOFError,ValueError):pass
  finally:c.close()
