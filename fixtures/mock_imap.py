#!/usr/bin/env python3
"""Synthetic read-only IMAP fixture. It supplies data; it is not a collector."""
import argparse,copy,json,socket
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=11430);p.add_argument('--stage',choices=['initial','shift','reset','truncated'],default='initial');p.add_argument('--log',default='imap-server.jsonl');a=p.parse_args()
root=Path(__file__).resolve().parent;state=copy.deepcopy(json.loads((root/'stages.json').read_text())[a.stage]); shifted=False
log=Path(a.log)
def event(**x):
 with log.open('a')as f:f.write(json.dumps(x,ensure_ascii=False)+'\n')
def choose(spec,msgs):
 out=set();mx=max([m['uid']for m in msgs]or[0])
 for item in spec.split(','):
  if ':'in item:
   lo,hi=item.split(':');lo=mx if lo=='*'else int(lo);hi=mx if hi=='*'else int(hi);out.update(range(min(lo,hi),max(lo,hi)+1))
  else:out.add(mx if item=='*'else int(item))
 return out
with socket.socket()as server:
 server.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);server.bind(('127.0.0.1',a.port));server.listen();print(json.dumps({'ready':True,'port':server.getsockname()[1],'stage':a.stage}),flush=True)
 while True:
  conn,_=server.accept();conn.settimeout(20)
  try:
   f=conn.makefile('rb');conn.sendall(b'* OK synthetic IMAP fixture ready\r\n')
   while True:
    line=f.readline()
    if not line:break
    parts=line.decode('ascii').strip().split(' ',2);tag,cmd=parts[:2];arg=parts[2]if len(parts)>2 else '';cmd=cmd.upper();event(command=cmd,args='demo credentials omitted'if cmd=='LOGIN'else arg)
    def send(s):conn.sendall(s.encode('ascii'))
    if cmd=='CAPABILITY':send('* CAPABILITY IMAP4rev1\r\n'+tag+' OK CAPABILITY\r\n')
    elif cmd=='LOGIN':send(tag+' OK LOGIN\r\n')
    elif cmd=='EXAMINE':send('* FLAGS (\\Seen \\Flagged)\r\n* '+str(len(state['messages']))+' EXISTS\r\n* OK [UIDVALIDITY '+str(state['uidvalidity'])+'] fixed\r\n* OK [UIDNEXT '+str(max([m['uid']for m in state['messages']]or[0])+1)+'] next\r\n'+tag+' OK [READ-ONLY] EXAMINE\r\n')
    elif cmd=='UID':
     sub,rest=arg.split(' ',1);sub=sub.upper()
     if sub=='SEARCH':send('* SEARCH '+ ' '.join(str(m['uid'])for m in state['messages'])+'\r\n'+tag+' OK SEARCH\r\n')
     elif sub=='FETCH':
      requested=choose(rest.split(' ',1)[0],state['messages'])
      if state.get('expunge_on_first_fetch')and not shifted:
       uid=state['expunge_on_first_fetch'];index=next(i for i,m in enumerate(state['messages'])if m['uid']==uid);state['messages'].pop(index);shifted=True;send('* '+str(index+1)+' EXPUNGE\r\n');event(expunged_uid=uid,sequence=index+1)
      for seq,m in enumerate(state['messages'],1):
       if m['uid']not in requested:continue
       raw=(root/'messages'/m['file']).read_bytes();prefix=f"* {seq} FETCH (UID {m['uid']} FLAGS ({' '.join(m['flags'])}) BODY[] {{{len(raw)}}}\r\n"
       send(prefix)
       if state.get('truncate_literal'):conn.sendall(raw[:len(raw)//2]);event(truncated_uid=m['uid']);raise EOFError('fixture truncation')
       conn.sendall(raw+b')\r\n');event(sent_uid=m['uid'],sequence=seq,bytes=len(raw))
      send(tag+' OK FETCH\r\n')
     else:send(tag+' BAD unsupported UID command\r\n')
    elif cmd=='NOOP':send(tag+' OK NOOP\r\n')
    elif cmd=='LOGOUT':send('* BYE fixture\r\n'+tag+' OK LOGOUT\r\n');break
    else:send(tag+' BAD unsupported command\r\n')
  except (OSError,EOFError,ValueError)as e:event(connection_end=type(e).__name__)
  finally:
   try:conn.shutdown(socket.SHUT_RDWR)
   except OSError:pass
   conn.close()
