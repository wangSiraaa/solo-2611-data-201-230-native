#!/usr/bin/env python3
"""A scripted MQTT3.1.1 broker fixture; not a general broker or receiver."""
import argparse,json,socket,struct
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=11883);p.add_argument('--scenario',choices=['standard','reset'],default='standard');p.add_argument('--log',default='mqtt-server.jsonl');a=p.parse_args();root=Path(__file__).resolve().parent
sessions=json.loads((root/'scenarios.json').read_text())[a.scenario]
def log(**v):
 with open(a.log,'a')as f:f.write(json.dumps(v)+'\n')
def exact(c,n):
 b=b''
 while len(b)<n:
  x=c.recv(n-len(b))
  if not x:raise EOFError()
  b+=x
 return b
def read(c):
 h=exact(c,1)[0];size=0;mult=1
 for _ in range(4):
  x=exact(c,1)[0];size+=(x&127)*mult
  if not x&128:break
  mult*=128
 else:raise ValueError('remaining length')
 body=exact(c,size);log(direction='received',type=h>>4,hex=body.hex());return h,body
def send(c,h,body=b''):
 n=len(body);r=b''
 while True:
  x=n%128;n//=128;r+=bytes([x|(128 if n else 0)])
  if not n:break
 c.sendall(bytes([h])+r+body);log(direction='sent',type=h>>4,hex=body.hex())
def auxiliary(c,h,b):
 if h>>4==8:
  pid=struct.unpack('!H',b[:2])[0];send(c,0x90,struct.pack('!HB',pid,2));return True
 if h>>4==12:send(c,0xd0);return True
 return False
with socket.socket()as s:
 s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('127.0.0.1',a.port));s.listen();print(json.dumps({'ready':True,'port':s.getsockname()[1]}),flush=True)
 for ordinal,session in enumerate(sessions):
  c,_=s.accept();c.settimeout(30)
  try:
   h,b=read(c)
   if h>>4!=1 or b[:7]!=b'\0\4MQTT\4' or b[7]&2:raise ValueError('CONNECT must use MQTT3.1.1 and clean_session=false')
   send(c,0x20,bytes([int(session['session_present']),0]));log(session=ordinal,session_present=session['session_present'])
   if not session['session_present']:
    while True:
     h,b=read(c)
     if auxiliary(c,h,b)and h>>4==8:break
   for action in session['actions']:
    if action.get('close'):log(injected_disconnect=True);break
    if action.get('send')=='publish':
     topic=b'telemetry/line';body=struct.pack('!H',len(topic))+topic+struct.pack('!H',action['id'])+action['payload'].encode();send(c,0x34|(8 if action['dup']else 0),body)
    elif action.get('send')=='pubrel':send(c,0x62,struct.pack('!H',action['id']))
    elif 'expect'in action:
     typ={'pubrec':5,'pubcomp':7}[action['expect']]
     while True:
      h,b=read(c)
      if auxiliary(c,h,b):continue
      if h>>4==5 and typ==7:continue # duplicate PUBREC for duplicate PUBLISH
      if h>>4!=typ or b!=struct.pack('!H',action['id']):raise ValueError('unexpected acknowledgement')
      break
  except (OSError,ValueError,EOFError)as e:log(error=type(e).__name__,detail=str(e))
  finally:c.close()
 print(json.dumps({'fixture_complete':True}),flush=True)
