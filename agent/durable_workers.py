"""Experimental durable worker primitives for Hermes H1.

A durable worker is persistent identity plus transcript. Each turn launches a
fresh existing Hermes subagent activation. No AIAgent, credential, callback,
thread, or socket is serialized. The module is UI agnostic and the bundled
``durable-workers`` plugin is opt in.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

WORKER_STATES = {"DORMANT", "RUNNING", "FAILED", "DISABLED"}
MESSAGE_STATES = {"PENDING", "PROCESSING", "CONSUMED", "FAILED", "COMPLETE"}
TASK_STATES = {"pending", "in_progress", "completed", "failed", "cancelled"}

class DurableWorkerError(ValueError): pass
class DurableWorkerAuthorizationError(DurableWorkerError): pass
class DurableWorkerConflictError(DurableWorkerError): pass

def _id(prefix: str) -> str: return f"{prefix}_{uuid.uuid4().hex}"
def _now() -> float: return time.time()
def _alive(pid: Optional[int]) -> bool:
    if not pid or pid <= 0: return False
    if pid == os.getpid(): return True
    try: os.kill(pid, 0)
    except ProcessLookupError: return False
    except PermissionError: return True
    except OSError: return False
    return True

def _default_path() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "durable-workers.db"

class DurableWorkerStore:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path else _default_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        self.recover_abandoned_activations()

    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=1.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try: db.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError: pass
        return db

    def _init_schema(self):
        with self._db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS durable_workers(
              worker_id TEXT PRIMARY KEY,parent_session_id TEXT NOT NULL,label TEXT NOT NULL,
              status TEXT NOT NULL,role TEXT NOT NULL,model TEXT,toolsets_json TEXT,
              created_at REAL NOT NULL,updated_at REAL NOT NULL,revision INTEGER NOT NULL DEFAULT 1,
              last_activation_id TEXT);
            CREATE INDEX IF NOT EXISTS idx_dw_parent ON durable_workers(parent_session_id,updated_at DESC);
            CREATE TABLE IF NOT EXISTS durable_worker_messages(
              message_id TEXT PRIMARY KEY,worker_id TEXT NOT NULL REFERENCES durable_workers(worker_id) ON DELETE CASCADE,
              direction TEXT NOT NULL,content TEXT NOT NULL,state TEXT NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_dwm_pending ON durable_worker_messages(worker_id,state,created_at,message_id);
            CREATE TABLE IF NOT EXISTS durable_worker_activations(
              activation_id TEXT PRIMARY KEY,worker_id TEXT NOT NULL REFERENCES durable_workers(worker_id) ON DELETE CASCADE,
              message_id TEXT REFERENCES durable_worker_messages(message_id) ON DELETE SET NULL,subagent_id TEXT,state TEXT NOT NULL,
              started_at REAL NOT NULL,completed_at REAL,summary TEXT,error TEXT,owner_pid INTEGER);
            CREATE INDEX IF NOT EXISTS idx_dwa_worker ON durable_worker_activations(worker_id,started_at DESC);
            CREATE TABLE IF NOT EXISTS durable_worker_tasks(
              task_id TEXT PRIMARY KEY,parent_session_id TEXT NOT NULL,worker_id TEXT REFERENCES durable_workers(worker_id) ON DELETE SET NULL,
              subject TEXT NOT NULL,description TEXT NOT NULL DEFAULT '',status TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,
              created_at REAL NOT NULL,updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS durable_worker_task_dependencies(
              task_id TEXT NOT NULL REFERENCES durable_worker_tasks(task_id) ON DELETE CASCADE,
              blocked_by_task_id TEXT NOT NULL REFERENCES durable_worker_tasks(task_id) ON DELETE CASCADE,
              PRIMARY KEY(task_id,blocked_by_task_id));
            """)

    def _owned_worker(self, db, parent: str, worker_id: str):
        row = db.execute("SELECT * FROM durable_workers WHERE worker_id=? AND parent_session_id=?", (worker_id,parent)).fetchone()
        if row is None: raise DurableWorkerAuthorizationError("Durable worker not found for the active parent session.")
        return row

    @staticmethod
    def _worker(row):
        d = dict(row); d["toolsets"] = json.loads(d.pop("toolsets_json") or "[]"); return d

    def create_worker(self, parent: str, *, label: str, role="leaf", model=None, toolsets: Optional[Iterable[str]]=None, max_workers=64):
        label = str(label).strip()
        if not label or len(label) > 160: raise DurableWorkerError("label must contain 1..160 characters")
        now, worker_id = _now(), _id("dw")
        with self._db() as db:
            count = db.execute("SELECT COUNT(*) FROM durable_workers WHERE parent_session_id=?",(parent,)).fetchone()[0]
            if count >= max_workers: raise DurableWorkerConflictError("durable worker limit reached for parent session")
            db.execute("INSERT INTO durable_workers VALUES(?,?,?,?,?,?,?,?,?,1,NULL)",
                       (worker_id,parent,label,"DORMANT",role,model,json.dumps(list(toolsets or [])),now,now))
        return self.get_worker(parent, worker_id)

    def get_worker(self, parent: str, worker_id: str):
        with self._db() as db: return self._worker(self._owned_worker(db,parent,worker_id))

    def list_workers(self, parent: str):
        with self._db() as db:
            return [self._worker(r) for r in db.execute("SELECT * FROM durable_workers WHERE parent_session_id=? ORDER BY updated_at DESC",(parent,))]

    def _set_worker(self, parent: str, worker_id: str, status: str, activation_id=None):
        if status not in WORKER_STATES: raise DurableWorkerError(f"unsupported worker state: {status}")
        with self._db() as db:
            self._owned_worker(db,parent,worker_id)
            db.execute("UPDATE durable_workers SET status=?,updated_at=?,revision=revision+1,last_activation_id=COALESCE(?,last_activation_id) WHERE worker_id=?",
                       (status,_now(),activation_id,worker_id))

    def enqueue_message(self, parent: str, worker_id: str, content: str, *, message_id=None):
        content = str(content or "").strip()
        if not content or len(content)>32000: raise DurableWorkerError("message must contain 1..32000 characters")
        message_id, now = str(message_id or _id("dwm")), _now()
        with self._db() as db:
            self._owned_worker(db,parent,worker_id)
            old = db.execute("SELECT * FROM durable_worker_messages WHERE message_id=?",(message_id,)).fetchone()
            if old:
                if old["worker_id"] != worker_id or old["direction"] != "parent" or old["content"] != content:
                    raise DurableWorkerConflictError("message_id already exists with different durable content")
                return {**dict(old),"created":False}
            db.execute("INSERT INTO durable_worker_messages VALUES(?,?, 'parent',?,'PENDING',?,?)",(message_id,worker_id,content,now,now))
        return {"message_id":message_id,"worker_id":worker_id,"direction":"parent","content":content,"state":"PENDING","created_at":now,"updated_at":now,"created":True}

    def claim_next_message(self, parent: str, worker_id: str):
        db=self._db()
        try:
            db.execute("BEGIN IMMEDIATE"); self._owned_worker(db,parent,worker_id)
            row=db.execute("SELECT * FROM durable_worker_messages WHERE worker_id=? AND direction='parent' AND state='PENDING' ORDER BY created_at,message_id LIMIT 1",(worker_id,)).fetchone()
            if row is None: db.commit(); return None
            if db.execute("UPDATE durable_worker_messages SET state='PROCESSING',updated_at=? WHERE message_id=? AND state='PENDING'",(_now(),row["message_id"])).rowcount != 1:
                db.rollback(); return None
            db.commit(); out=dict(row); out["state"]="PROCESSING"; return out
        except Exception: db.rollback(); raise
        finally: db.close()

    def mark_message(self, message_id: str, state: str):
        if state not in MESSAGE_STATES: raise DurableWorkerError(f"unsupported message state: {state}")
        with self._db() as db: db.execute("UPDATE durable_worker_messages SET state=?,updated_at=? WHERE message_id=?",(state,_now(),message_id))

    def append_report(self, worker_id: str, content: str):
        now, mid = _now(), _id("dwm"); content=str(content or "")[:32000]
        with self._db() as db: db.execute("INSERT INTO durable_worker_messages VALUES(?,?,'worker',?,'COMPLETE',?,?)",(mid,worker_id,content,now,now))
        return {"message_id":mid,"worker_id":worker_id,"content":content}

    def render_context(self, parent: str, worker_id: str, *, exclude_message_id=None, max_chars=24000):
        w=self.get_worker(parent,worker_id)
        with self._db() as db:
            rows=db.execute("SELECT message_id,direction,content FROM durable_worker_messages WHERE worker_id=? AND state IN('CONSUMED','COMPLETE') ORDER BY created_at,message_id",(worker_id,)).fetchall()
        lines=[f"Durable worker: {w['label']} ({worker_id})","This is a new runtime activation of the same durable worker identity.","Use the prior durable transcript as context; do not claim the Python process itself persisted.","","Prior durable transcript:"]
        for r in rows:
            if r["message_id"]==exclude_message_id: continue
            lines.append(("PARENT" if r["direction"]=="parent" else "WORKER")+": "+r["content"])
        text="\n".join(lines)
        return text if len(text)<=max_chars else "\n".join(lines[:5])+"\n[older transcript truncated]\n"+text[-max_chars:]

    def start_activation(self,parent: str,worker_id: str,message_id: str):
        aid,now=_id("dwa"),_now()
        with self._db() as db:
            self._owned_worker(db,parent,worker_id)
            db.execute("INSERT INTO durable_worker_activations(activation_id,worker_id,message_id,state,started_at,owner_pid) VALUES(?,?,?,'STARTING',?,?)",(aid,worker_id,message_id,now,os.getpid()))
        self._set_worker(parent,worker_id,"RUNNING",aid); return aid

    def bind_activation(self, activation_id: str, subagent_id: str):
        with self._db() as db: db.execute("UPDATE durable_worker_activations SET subagent_id=?,state='RUNNING' WHERE activation_id=?",(subagent_id,activation_id))

    def finish_activation(self,parent: str,worker_id: str,activation_id: str,*,state: str,summary=None,error=None):
        with self._db() as db: db.execute("UPDATE durable_worker_activations SET state=?,completed_at=?,summary=?,error=? WHERE activation_id=? AND worker_id=?",(state,_now(),summary,error,activation_id,worker_id))
        self._set_worker(parent,worker_id,"DORMANT" if state=="SUCCEEDED" else "FAILED",activation_id)

    def list_activations(self,parent: str,worker_id: str):
        with self._db() as db:
            self._owned_worker(db,parent,worker_id)
            return [dict(r) for r in db.execute("SELECT * FROM durable_worker_activations WHERE worker_id=? ORDER BY started_at",(worker_id,))]

    def list_messages(self,parent: str,worker_id: str,*,direction=None):
        with self._db() as db:
            self._owned_worker(db,parent,worker_id)
            sql="SELECT * FROM durable_worker_messages WHERE worker_id=?"; args=[worker_id]
            if direction: sql+=" AND direction=?"; args.append(direction)
            sql+=" ORDER BY created_at,message_id"
            return [dict(r) for r in db.execute(sql,args)]

    def recover_abandoned_activations(self):
        recovered=0
        with self._db() as db:
            rows=db.execute("SELECT activation_id,worker_id,message_id,owner_pid FROM durable_worker_activations WHERE state IN('STARTING','RUNNING')").fetchall()
            for r in rows:
                if _alive(r["owner_pid"]): continue
                now=_now(); recovered+=1
                db.execute("UPDATE durable_worker_activations SET state='ABANDONED',completed_at=?,error='owner process disappeared' WHERE activation_id=?",(now,r["activation_id"]))
                if r["message_id"]: db.execute("UPDATE durable_worker_messages SET state='PENDING',updated_at=? WHERE message_id=? AND state='PROCESSING'",(now,r["message_id"]))
                db.execute("UPDATE durable_workers SET status='DORMANT',updated_at=?,revision=revision+1 WHERE worker_id=? AND status='RUNNING'",(now,r["worker_id"]))
        return recovered

    def _owned_task(self,db,parent: str,task_id: str):
        row=db.execute("SELECT * FROM durable_worker_tasks WHERE task_id=? AND parent_session_id=?",(task_id,parent)).fetchone()
        if row is None: raise DurableWorkerAuthorizationError("Durable task not found for the active parent session.")
        return row

    def create_task(self,parent: str,*,subject: str,description="",worker_id=None):
        subject=str(subject).strip(); description=str(description or "")
        if not subject or len(subject)>300 or len(description)>16000: raise DurableWorkerError("invalid task subject or description")
        if worker_id: self.get_worker(parent,worker_id)
        tid,now=_id("dwt"),_now()
        with self._db() as db: db.execute("INSERT INTO durable_worker_tasks VALUES(?,?,?,?,?,'pending',1,?,?)",(tid,parent,worker_id,subject,description,now,now))
        return self.get_task(parent,tid)

    def _task(self,db,row):
        d=dict(row); blockers=db.execute("SELECT blocked_by_task_id FROM durable_worker_task_dependencies WHERE task_id=? ORDER BY blocked_by_task_id",(d["task_id"],)).fetchall(); d["blocked_by"]=[r[0] for r in blockers]
        if d["status"]!="pending": d["ready"]=False
        elif not blockers: d["ready"]=True
        else:
            qs=",".join("?" for _ in blockers); states={r["task_id"]:r["status"] for r in db.execute(f"SELECT task_id,status FROM durable_worker_tasks WHERE task_id IN({qs})",tuple(r[0] for r in blockers))}
            d["ready"]=all(states.get(r[0])=="completed" for r in blockers)
        return d

    def get_task(self,parent: str,task_id: str):
        with self._db() as db: return self._task(db,self._owned_task(db,parent,task_id))

    def list_tasks(self,parent: str):
        with self._db() as db: return [self._task(db,r) for r in db.execute("SELECT * FROM durable_worker_tasks WHERE parent_session_id=? ORDER BY created_at,task_id",(parent,))]

    def add_task_dependency(self,parent: str,task_id: str,blocked_by: str):
        if task_id==blocked_by: raise DurableWorkerConflictError("task dependency cycle")
        with self._db() as db:
            self._owned_task(db,parent,task_id); self._owned_task(db,parent,blocked_by)
            graph={}
            for a,b in db.execute("SELECT task_id,blocked_by_task_id FROM durable_worker_task_dependencies JOIN durable_worker_tasks USING(task_id) WHERE parent_session_id=?",(parent,)): graph.setdefault(a,set()).add(b)
            graph.setdefault(task_id,set()).add(blocked_by)
            def reaches(node,target,stack):
                if node==target: return True
                if node in stack: return False
                return any(reaches(n,target,stack|{node}) for n in graph.get(node,()))
            if reaches(blocked_by,task_id,set()): raise DurableWorkerConflictError("task dependency cycle")
            db.execute("INSERT OR IGNORE INTO durable_worker_task_dependencies VALUES(?,?)",(task_id,blocked_by))
        return self.get_task(parent,task_id)

    def update_task(self,parent: str,task_id: str,*,status: str,expected_revision=None):
        if status not in TASK_STATES: raise DurableWorkerError(f"unsupported task status: {status}")
        db=self._db()
        try:
            db.execute("BEGIN IMMEDIATE"); row=self._owned_task(db,parent,task_id)
            if expected_revision is not None and row["revision"]!=int(expected_revision): raise DurableWorkerConflictError(f"task revision changed (expected {expected_revision}, actual {row['revision']})")
            db.execute("UPDATE durable_worker_tasks SET status=?,revision=revision+1,updated_at=? WHERE task_id=?",(status,_now(),task_id)); db.commit()
        except Exception: db.rollback(); raise
        finally: db.close()
        return self.get_task(parent,task_id)

class DurableWorkerService:
    """Durable identity layered on Hermes' existing subagent lifecycle."""
    def __init__(self,store: DurableWorkerStore,lifecycle: Any,parent_resolver: Callable[[],Any]):
        self.store,self.lifecycle,self.parent_resolver=store,lifecycle,parent_resolver
    def parent_session_id(self):
        parent=self.parent_resolver(); sid=str(getattr(parent,"session_id","") or "").strip()
        if not sid: raise DurableWorkerAuthorizationError("Durable workers require an active Hermes parent session.")
        return sid
    def create_worker(self,**kwargs): return self.store.create_worker(self.parent_session_id(),**kwargs)
    def list_workers(self): return self.store.list_workers(self.parent_session_id())
    def get_worker(self,wid): return self.store.get_worker(self.parent_session_id(),wid)
    def enqueue(self,wid,message,*,message_id=None): return self.store.enqueue_message(self.parent_session_id(),wid,message,message_id=message_id)
    def send(self,wid,message,*,message_id=None,timeout_seconds=None): return {"queued":self.enqueue(wid,message,message_id=message_id),"activation":self.run_next(wid,timeout_seconds=timeout_seconds)}
    def run_next(self,wid,*,timeout_seconds=None):
        parent=self.parent_session_id(); worker=self.store.get_worker(parent,wid); msg=self.store.claim_next_message(parent,wid)
        if msg is None: return {"worker_id":wid,"status":"NO_PENDING_MESSAGE"}
        aid=self.store.start_activation(parent,wid,msg["message_id"]); context=self.store.render_context(parent,wid,exclude_message_id=msg["message_id"])
        try:
            from agent.subagent_lifecycle import SubagentLaunchRequest
            handle=self.lifecycle.launch(SubagentLaunchRequest(goal=msg["content"],context=context,role=worker["role"],model=worker["model"],allowed_toolsets=tuple(worker["toolsets"]) or None,parent_session_id=parent,correlation_id=aid,metadata={"durable_worker_id":wid,"durable_activation_id":aid},timeout_seconds=timeout_seconds))
            self.store.bind_activation(aid,handle.subagent_id)
            terminal=self.lifecycle.wait(handle,timeout_seconds=timeout_seconds)
            if not terminal.completed:
                try: self.lifecycle.cancel(handle,reason="durable worker activation timeout")
                except Exception: pass
                self.store.mark_message(msg["message_id"],"FAILED"); self.store.finish_activation(parent,wid,aid,state="TIMED_OUT",error="activation timeout")
                return {"worker_id":wid,"activation_id":aid,"subagent_id":handle.subagent_id,"status":"TIMED_OUT"}
            result=self.lifecycle.result(handle); state=getattr(terminal.state,"value",str(terminal.state)); summary=getattr(result,"summary",None); error=getattr(result,"error_message",None)
            if state=="SUCCEEDED" and getattr(result,"ready",False):
                text=str(summary or "(completed without summary)")[:32000]; self.store.mark_message(msg["message_id"],"CONSUMED"); report=self.store.append_report(wid,text); self.store.finish_activation(parent,wid,aid,state="SUCCEEDED",summary=text)
                return {"worker_id":wid,"activation_id":aid,"subagent_id":handle.subagent_id,"status":"SUCCEEDED","summary":text,"report_message_id":report["message_id"]}
            err=str(error or state or "activation failed")[:32000]; self.store.mark_message(msg["message_id"],"FAILED"); self.store.finish_activation(parent,wid,aid,state=state or "FAILED",error=err)
            return {"worker_id":wid,"activation_id":aid,"subagent_id":handle.subagent_id,"status":state or "FAILED","error":err}
        except Exception as exc:
            self.store.mark_message(msg["message_id"],"PENDING"); self.store.finish_activation(parent,wid,aid,state="FAILED_TO_START",error=str(exc)[:32000]); raise
