import argparse,hashlib,json,pathlib,socket,threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer

def fingerprint(d):return hashlib.sha256(json.dumps(d,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
def main():
 p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=0);p.add_argument('--scenario',required=True);p.add_argument('--ready',required=True);a=p.parse_args();cfg=json.loads(pathlib.Path(a.scenario).read_text());lock=threading.Lock();docs={};attempts=[];writes=[];once=set();drop_done=False
 for row in cfg.get('initial',[]):
  body=dict(row['document']);body['ingest_fingerprint']=fingerprint(row['document']);docs[(row['index'],row['id'])]={'version':row['version'],'source':body}
 class H(BaseHTTPRequestHandler):
  def log_message(self,*args):pass
  def reply(self,obj,status=200):
   raw=json.dumps(obj,ensure_ascii=False).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
  def do_GET(self):
   if self.path!='/__inspect':return self.reply({'error':'not found'},404)
   with lock:self.reply({'docs':[{'index':i,'id':d,**v} for (i,d),v in sorted(docs.items())],'attempts':list(attempts),'writes':list(writes),'drop_done':drop_done})
  def do_POST(self):
   nonlocal drop_done
   raw=self.rfile.read(int(self.headers.get('Content-Length',0)))
   try:
    if self.path=='/_mget':
     req=json.loads(raw);out=[]
     with lock:
      for item in req['docs']:
       key=(item['_index'],item['_id']);v=docs.get(key);d={'_index':key[0],'_id':key[1],'found':v is not None}
       if v:d.update(_version=v['version'],_source=v['source'])
       out.append(d)
     return self.reply({'docs':out})
    if self.path!='/_bulk':return self.reply({'error':'not found'},404)
    lines=raw.decode().splitlines()
    if len(lines)%2:raise ValueError('odd NDJSON line count')
    parsed=[(json.loads(lines[i])['index'],json.loads(lines[i+1])) for i in range(0,len(lines),2)];out=[];disconnect=False
    with lock:
     dropping=not drop_done and cfg.get('drop_first_bulk_after',0)>0
     for meta,body in parsed:
      key=(meta['_index'],meta['_id']);version=meta['version'];attempts.append({'index':key[0],'id':key[1],'version':version,'source':body});error=None
      if meta.get('version_type')!='external' or not isinstance(version,int) or isinstance(version,bool) or version<=0:status=400;error='bad metadata'
      elif key[1] in cfg.get('transient_429_ids',[]) and key not in once:once.add(key);status=429;error='rejected execution'
      elif not isinstance(body.get('sku'),str) or type(body.get('amount_cents')) is not int:status=400;error='mapper_parsing_exception'
      elif key in docs and version<=docs[key]['version']:status=409;error='version_conflict_engine_exception'
      else:docs[key]={'version':version,'source':body};writes.append({'index':key[0],'id':key[1],'version':version,'source':body});status=201
      entry={'_index':key[0],'_id':key[1],'status':status}
      if error:entry['error']={'type':error}
      else:entry['_version']=version
      out.append({'index':entry})
      if dropping and len(out)>=cfg['drop_first_bulk_after']:drop_done=True;disconnect=True;break
    if disconnect:
     self.close_connection=True
     try:self.connection.shutdown(socket.SHUT_RDWR)
     except OSError:pass
     self.connection.close();return
    return self.reply({'errors':any(x['index']['status']>=400 for x in out),'items':out})
   except (ValueError,KeyError,TypeError) as exc:self.reply({'error':str(exc)},400)
 server=ThreadingHTTPServer(('127.0.0.1',a.port),H);ready=pathlib.Path(a.ready);ready.parent.mkdir(parents=True,exist_ok=True);ready.write_text(json.dumps({'base_url':f'http://127.0.0.1:{server.server_port}'}));print(ready.read_text(),flush=True);server.serve_forever()
if __name__=='__main__':main()
