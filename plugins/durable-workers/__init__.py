"""Opt-in H1 durable-workers plugin."""
from __future__ import annotations

import json
import shlex
from typing import Any, Optional

from agent.durable_workers import DurableWorkerError, DurableWorkerService, DurableWorkerStore
from agent.subagent_lifecycle import get_active_subagent_parent

_TOOL_SCHEMA = {
    "name": "durable_worker",
    "description": "Create and operate experimental durable worker identities backed by persistent inbox, activation history and task dependencies.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type":"string","enum":["create","list","show","enqueue","send","run_next","reports","task_create","task_depend","task_update","task_list"]},
            "worker_id": {"type":"string"}, "label": {"type":"string"},
            "role": {"type":"string","enum":["leaf","orchestrator"]}, "model": {"type":"string"},
            "toolsets": {"type":"array","items":{"type":"string"}}, "message": {"type":"string"},
            "message_id": {"type":"string"},
            "task_id": {"type":"string"}, "blocked_by_task_id": {"type":"string"},
            "subject": {"type":"string"}, "description": {"type":"string"},
            "status": {"type":"string","enum":["pending","in_progress","completed","failed","cancelled"]},
            "expected_revision": {"type":"integer","minimum":1}
        },
        "required": ["action"]
    }
}

def _require(params: dict[str, Any], key: str) -> str:
    value=str(params.get(key) or "").strip()
    if not value: raise DurableWorkerError(f"Missing required field: {key}")
    return value

def register(ctx) -> None:
    store=DurableWorkerStore()
    service=DurableWorkerService(store,ctx.subagent_lifecycle,get_active_subagent_parent)
    def parent(): return service.parent_session_id()
    def handle(params: dict[str,Any], **_kwargs: Any) -> str:
        action=str(params.get("action") or "")
        try:
            if action=="create": result=service.create_worker(label=params.get("label") or "worker",role=params.get("role") or "leaf",model=params.get("model"),toolsets=params.get("toolsets"))
            elif action=="list": result={"workers":service.list_workers()}
            elif action=="show":
                wid=_require(params,"worker_id"); result={"worker":service.get_worker(wid),"activations":store.list_activations(parent(),wid),"messages":store.list_messages(parent(),wid)}
            elif action=="enqueue": result=service.enqueue(_require(params,"worker_id"),_require(params,"message"),message_id=params.get("message_id"))
            elif action=="send": result=service.send(_require(params,"worker_id"),_require(params,"message"),message_id=params.get("message_id"))
            elif action=="run_next": result=service.run_next(_require(params,"worker_id"))
            elif action=="reports": result={"activations":store.list_activations(parent(),_require(params,"worker_id")),"messages":store.list_messages(parent(),_require(params,"worker_id"),direction="worker")}
            elif action=="task_create": result=store.create_task(parent(),subject=_require(params,"subject"),description=str(params.get("description") or ""),worker_id=params.get("worker_id"))
            elif action=="task_depend": result=store.add_task_dependency(parent(),_require(params,"task_id"),_require(params,"blocked_by_task_id"))
            elif action=="task_update": result=store.update_task(parent(),_require(params,"task_id"),status=_require(params,"status"),expected_revision=params.get("expected_revision"))
            elif action=="task_list": result={"tasks":store.list_tasks(parent())}
            else: raise DurableWorkerError(f"Unsupported action: {action}")
            return json.dumps({"success":True,"result":result},default=str)
        except DurableWorkerError as exc:
            return json.dumps({"success":False,"error":str(exc)})
    ctx.register_tool(name="durable_worker",toolset="durable_workers",schema=_TOOL_SCHEMA,handler=handle)
    def slash(raw_args: str) -> Optional[str]:
        try: argv=shlex.split(raw_args or "")
        except ValueError as exc: return f"/workers parse error: {exc}"
        if not argv or argv[0] in {"list","ls"}: return ctx.dispatch_tool("durable_worker",{"action":"list"})
        if argv[0]=="show" and len(argv)==2: return ctx.dispatch_tool("durable_worker",{"action":"show","worker_id":argv[1]})
        if argv[0]=="tasks": return ctx.dispatch_tool("durable_worker",{"action":"task_list"})
        if argv[0]=="send" and len(argv)>=3: return ctx.dispatch_tool("durable_worker",{"action":"send","worker_id":argv[1],"message":" ".join(argv[2:])})
        return "Usage: /workers [list | show <worker_id> | tasks | send <worker_id> <message>]"
    ctx.register_command("workers",handler=slash,description="Inspect and message experimental durable workers.",args_hint="[list|show <id>|tasks|send <id> <message>]")
