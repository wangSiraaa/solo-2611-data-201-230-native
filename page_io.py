"""Test disk backend only; implement your buffer pool in another module."""
from pathlib import Path
class PageIO:
    page_size = 128
    def __init__(self, path):
        self.path=Path(path); self.events=[]; self.fail_writes=0
    def read(self, page_id):
        with self.path.open('rb') as f:
            f.seek(page_id*self.page_size); data=f.read(self.page_size)
        if len(data)!=self.page_size: raise ValueError('page outside file')
        self.events.append({'op':'read','page_id':page_id}); return data
    def write(self, page_id, data):
        if len(data)!=self.page_size: raise ValueError('bad page length')
        if self.fail_writes:
            self.fail_writes-=1; self.events.append({'op':'write_failed','page_id':page_id}); raise OSError('injected write failure')
        with self.path.open('r+b') as f:
            f.seek(page_id*self.page_size); f.write(data); f.flush()
        self.events.append({'op':'write','page_id':page_id})
