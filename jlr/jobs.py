"""Bounded, session-owned V2 jobs shared by sync and async HTTP callers."""
import copy
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

class JobError(ValueError):
    def __init__(self,message,code=409):super().__init__(message);self.code=code

class JobManager:
    def __init__(self):
        # Two workers: one driver's slow larger-model request does not hold up everyone else's.
        self.jobs={};self.lock=threading.RLock();self.executor=ThreadPoolExecutor(max_workers=2)
    def public(self,j):return copy.deepcopy({k:v for k,v in j.items() if k not in {'future','cancel','fingerprint','key'}})
    def prune(self):
        now=time.time()
        finished=sorted((j.get('finished_at',now),k) for k,j in self.jobs.items() if j['status'] in {'completed','failed','cancelled'})
        for n,(when,k) in enumerate(finished):
            if now-when>3600 or n<len(finished)-100:self.jobs.pop(k,None)
    def submit(self,sid,operation,payload,fn,key=None):
        import json
        fingerprint=json.dumps([operation,payload],sort_keys=True)
        with self.lock:
            self.prune()
            for j in self.jobs.values():
                if key and j['session_id']==sid and j.get('key')==key:
                    if j['fingerprint']!=fingerprint:raise JobError('Idempotency key conflict')
                    return self.public(j)
            active=[j for j in self.jobs.values() if j['status'] in {'queued','running','cancelling'}]
            # A job being cancelled no longer owns its session (its result is discarded), but it still
            # occupies a slot until it stops, so it counts towards the global limit.
            if any(j['session_id']==sid and j['status']!='cancelling' for j in active):raise JobError('Session already has an active job')
            if len(active)>=4:raise JobError('Request queue is full',429)
            jid=uuid.uuid4().hex
            j={'job_id':jid,'session_id':sid,'status':'queued','created_at':time.time(),'operation':operation,
               'key':key,'fingerprint':fingerprint,'cancel':threading.Event(),'poll_url':'/api/v2/jobs/'+jid}
            self.jobs[jid]=j
            def run():
                with self.lock:
                    if j['cancel'].is_set():j.update(status='cancelled',finished_at=time.time());return
                    j.update(status='running',started_at=time.time())
                try:
                    result=fn(j['cancel'])
                    with self.lock:j.update(status='completed',result=result,finished_at=time.time())
                except Exception as exc:
                    with self.lock:j.update(status='failed',error=str(exc),finished_at=time.time())
            j['future']=self.executor.submit(run)
            return self.public(j)
    def get(self,jid,sid):
        with self.lock:
            self.prune();j=self.jobs.get(jid)
            if not j or j['session_id']!=sid:raise JobError('V2 job not found',404)
            return self.public(j)
    def cancel(self,jid,sid):
        with self.lock:
            self.get(jid,sid);j=self.jobs[jid]
            if j['status'] in {'completed','failed','cancelled'}:return self.public(j)
            j['cancel'].set()
            if j['future'].cancel():j.update(status='cancelled',finished_at=time.time())
            else:j['status']='cancelling'
            return self.public(j)
    def cancel_session(self,sid):
        with self.lock:
            for j in list(self.jobs.values()):
                if j['session_id']==sid:self.cancel(j['job_id'],sid)

JOBS=JobManager()
