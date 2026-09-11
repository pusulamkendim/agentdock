import concurrent.futures
import base64
import hashlib
import mimetypes
import json
import os
import re
import select
import queue
import signal
import shutil
import shlex
import sys
import sqlite3
import subprocess
import threading
import time
import uuid
import webbrowser
import fnmatch
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

def open_terminal_at(path):
    path=str(Path(path).expanduser().resolve())
    if not Path(path).is_dir():
        raise ValueError('Terminal klasörü artık mevcut değil')
    if sys.platform == 'darwin':
        script=f'tell application "Terminal" to do script "cd {shlex.quote(path)}"'
        subprocess.Popen(['osascript','-e',script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    raise ValueError('Open Terminal currently supports macOS only')

def run_single_task(task_id):
    task = one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not task:
        return
    plan = one("SELECT * FROM plans WHERE id=?", (task["plan_id"],))
    workspace = task.get("workspace") or plan["workspace"]
    result = run_task_once(plan, task, workspace, force_mode=task["mode"])
    if result["ok"]:
        execute("UPDATE tasks SET status=? WHERE id=?", ("done", task_id))

def run_manual_followup_message(message_id):
    msg=one("SELECT * FROM task_messages WHERE id=?",(message_id,))
    if not msg: return
    execute("UPDATE task_messages SET status=? WHERE id=?",('sending',message_id))
    try:
        run_manual_followup(msg['task_id'], msg.get('text') or 'Continue.', safe_json(msg.get('attachments_json'), []))
        execute("UPDATE task_messages SET status=?,error=? WHERE id=?",('delivered','',message_id))
    except Exception as e:
        execute("UPDATE task_messages SET status=?,error=? WHERE id=?",('failed',str(e),message_id))


def manual_followup_delivery_status(task_id):
    """Choose delivery based on a live runner, not a historical task status."""
    with APP_SERVER_CONTROLS_LOCK:
        if task_id in APP_SERVER_CONTROLS:
            return "sending"
    with RUNNERS_LOCK:
        if RUNNERS.get(task_id):
            return "queued"
    # An executed task can still have a resumable worker conversation. Start
    # the manual turn now; task execution state is intentionally independent.
    return "sending"

class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        rel = urlparse(path).path.lstrip("/") or "index.html"
        static_root = STATIC.resolve()
        candidate = (static_root / rel).resolve()
        if candidate != static_root and static_root not in candidate.parents:
            return str(static_root / "__agentdock_missing_file__")
        return str(candidate)

    def log_message(self, fmt, *args):
        pass

    def send_json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/api/health":
            engines = engine_status()
            return self.send_json({
                "ok": True,
                "version": "0.12.0",
                "db": str(DB),
                "engines": engines,
                "codex_transport": CODEX_TRANSPORT,
                "server_time": now(),
            })
        if p == "/api/state":
            plans = rows("SELECT * FROM plans ORDER BY created_at DESC LIMIT 100")
            for pl in plans:
                pl["tasks"] = rows("SELECT * FROM tasks WHERE plan_id=? ORDER BY seq", (pl["id"],))
                pl["repo"] = repo_info(pl["workspace"])
                pl["attachments"] = rows("SELECT id,name,mime,path,created_at FROM attachments WHERE plan_id=? ORDER BY created_at", (pl["id"],))
                pl["evidence"] = safe_json(pl.get("evidence_json"), [])
                pl["questions"] = safe_json(pl.get("questions_json"), [])
                pl["workspace_snapshot"] = safe_json(pl.get("workspace_snapshot_json"), {})
                pl["pending_question"] = safe_json(pl.get("pending_question_json"), {})
                pl["consultations"] = plan_consultations(pl["id"])
            workspaces = [workspace_summary(w) for w in rows("SELECT * FROM workspaces ORDER BY last_opened_at DESC, created_at DESC")]
            return self.send_json(
                {
                    "agents": rows("SELECT * FROM agents ORDER BY created_at"),
                    "plans": plans,
                    "workspaces": workspaces,
                    "engines": engine_status(),
                    "quota": quota_status(),
                    "defaults": {
                        "orchestrator": DEFAULT_ORCHESTRATOR,
                        "worker": DEFAULT_WORKER,
                        "orchestrator_effort": DEFAULT_ORCHESTRATOR_EFFORT,
                        "worker_effort": DEFAULT_WORKER_EFFORT,
                        "orchestrator_tier": DEFAULT_ORCHESTRATOR_TIER,
                        "worker_tier": DEFAULT_WORKER_TIER,
                        "max_parallel": 4,
                        "recovery": RECOVERY_DEFAULTS,
                    },
                }
            )
        if p.startswith("/api/live/"):
            pid = p.split("/")[-1]
            plan = one("SELECT * FROM plans WHERE id=?", (pid,))
            if not plan:
                return self.send_json({"error": "plan not found"}, 404)
            plan["attachments"] = rows("SELECT id,name,mime,path,created_at FROM attachments WHERE plan_id=? ORDER BY created_at", (pid,))
            plan["evidence"] = safe_json(plan.get("evidence_json"), [])
            plan["questions"] = safe_json(plan.get("questions_json"), [])
            plan["workspace_snapshot"] = safe_json(plan.get("workspace_snapshot_json"), {})
            plan["pending_question"] = safe_json(plan.get("pending_question_json"), {})
            consultations = plan_consultations(pid)
            tasks = rows(
                """SELECT t.*, a.name AS agent_name, a.role AS agent_role, a.model AS agent_model,
                          a.reasoning_effort AS agent_effort, a.service_tier AS agent_tier
                   FROM tasks t LEFT JOIN agents a ON a.id=t.agent_id
                   WHERE t.plan_id=? ORDER BY t.seq""",
                (pid,),
            )
            for task in tasks:
                latest = rows(
                    "SELECT id,ts,stream,line FROM logs WHERE task_id=? ORDER BY id DESC LIMIT 18",
                    (task["id"],),
                )
                task["recent_logs"] = list(reversed(latest))
                agent_settings = {
                    "model": task.get("agent_model") or "",
                    "reasoning_effort": task.get("agent_effort") or "",
                    "service_tier": task.get("agent_tier") or "",
                }
                task["effective_model"] = task_model(plan, agent_settings, task)
                task["effective_effort"] = task_effort(plan, agent_settings, task)
                task["effective_tier"] = task_tier(plan, agent_settings, task)
                task["contract"] = safe_json(task.get("contract_json"), {})
            orch_logs = rows("SELECT id,ts,stream,line FROM logs WHERE task_id=? ORDER BY id DESC LIMIT 24", (orchestrator_log_id(pid),))
            orchestrator = {
                "id": orchestrator_log_id(pid),
                "status": "running" if plan.get("status") in ("planning", "preflight", "running") else plan.get("status"),
                "model": plan.get("orchestrator_used") or plan.get("orchestrator_model"),
                "reasoning_effort": plan.get("orchestrator_effort"),
                "service_tier": plan.get("orchestrator_tier"),
                "thread_id": plan.get("orchestrator_thread_id") or "",
                "generation": int(plan.get("orchestrator_generation") or 1),
                "turn_status": plan.get("orchestrator_turn_status") or "",
                "last_turn_id": plan.get("orchestrator_last_turn_id") or "",
                "last_error": plan.get("orchestrator_last_error") or "",
                "legacy_state": plan.get("legacy_orchestrator_status") or "",
                "consultations": consultations,
                "recent_logs": list(reversed(orch_logs)),
            }
            doctor_logs = rows("SELECT id,ts,stream,line FROM logs WHERE task_id=? ORDER BY id DESC LIMIT 80", (doctor_log_id(pid),))
            doctor_logs.reverse()
            doctor_logs = latest_log_segment(doctor_logs, "preflight started")
            doctor = {
                "id": doctor_log_id(pid),
                "status": plan.get("preflight_status") or "idle",
                "report": safe_json(plan.get("preflight_json"), {}),
                "recent_logs": doctor_logs,
            }
            q = quota_status()
            return self.send_json({"plan": plan, "tasks": tasks, "orchestrator": orchestrator, "consultations": consultations, "doctor": doctor, "quota": q, "mission_usage": mission_usage(plan, q), "server_time": now()})
        if p.startswith("/api/logs/"):
            tid = p.split("/api/logs/",1)[1]
            return self.send_json({"logs": rows("SELECT * FROM logs WHERE task_id=? ORDER BY id", (tid,))})
        if p.startswith("/api/events/"):
            tid = p.split("/api/events/",1)[1]
            session = latest_agent_session(tid)
            events = rows("SELECT id,session_id,ts,event_type,item_type,payload_json FROM agent_events WHERE task_id=? ORDER BY id DESC LIMIT 300", (tid,))
            events.reverse()
            for ev in events:
                ev["payload"] = safe_json(ev.pop("payload_json", "{}"), {})
            return self.send_json({"session": session, "events": events})
        if p.startswith("/api/messages/"):
            tid=p.split("/api/messages/",1)[1]
            return self.send_json({"messages":rows("SELECT * FROM task_messages WHERE task_id=? ORDER BY ts,id",(tid,))})
        if p.startswith("/api/timeline/"):
            tid = p.split("/api/timeline/", 1)[1]
            return self.send_json(timeline_for(tid))
        if p.startswith("/api/diff/"):
            tid = p.split("/api/diff/",1)[1]
            task = one("SELECT * FROM tasks WHERE id=?", (tid,))
            if not task:
                return self.send_json({"error":"task not found"},404)
            return self.send_json({"diff": task_diff(task)})
        if p.startswith("/api/plan-diff/"):
            pid = p.split("/api/plan-diff/", 1)[1]
            plan = one("SELECT * FROM plans WHERE id=?", (pid,))
            if not plan:
                return self.send_json({"error": "mission not found"}, 404)
            if plan.get("status") != "awaiting_apply":
                return self.send_json({"diff": "No pending integration diff for this mission."})
            try:
                return self.send_json({"diff": integration_patch(stored_integration_context(plan))})
            except Exception as exc:
                return self.send_json({"error": str(exc)}, 409)
        if p == "/api/workspace/browse":
            selected = choose_workspace_folder()
            if not selected:
                return self.send_json({"ok": True, "cancelled": True})
            info = repo_info(selected)
            return self.send_json({
                "ok": True,
                "path": selected,
                "name": Path(selected).name,
                "is_git": bool(info.get("is_git")),
                "branch": info.get("branch") or "",
            })
        if p == "/api/quota":
            return self.send_json(quota_status(force=True, wait=True))
        if p.startswith("/api/docs/"):
            pid = p.split("/")[-1]
            write_mission_docs(pid)
            pl = one("SELECT mission_dir FROM plans WHERE id=?", (pid,))
            return self.send_json({"mission_dir": pl.get("mission_dir") if pl else ""})
        return super().do_GET()

    def do_POST(self):
        p = urlparse(self.path).path
        try:
            data = self.body()
            if p == "/api/workspaces":
                ws = ensure_workspace(data.get("repo_path") or data.get("path") or "", data.get("name"))
                return self.send_json({"ok": True, "workspace": ws})

            if p == "/api/agents":
                aid = data.get("id") or str(uuid.uuid4())[:8]
                agent_model = (data.get("model") or "").strip()
                agent_effort = (data.get("reasoning_effort") or "").strip()
                agent_tier = (data.get("service_tier") or "").strip()
                if agent_model and agent_effort:
                    validate_runtime_config(agent_model, agent_effort, agent_tier or "default", "Worker profile")
                elif agent_tier and agent_tier not in VALID_TIERS:
                    raise ValueError(f"Worker profile: invalid speed {agent_tier}")
                execute(
                    "INSERT OR REPLACE INTO agents(id,name,role,engine,model,mode,created_at,reasoning_effort,service_tier) VALUES(?,?,?,?,?,?,COALESCE((SELECT created_at FROM agents WHERE id=?),?),?,?)",
                    (
                        aid,
                        data["name"],
                        data["role"],
                        "codex",
                        agent_model,
                        data.get("mode", "read"),
                        aid,
                        now(),
                        agent_effort,
                        agent_tier,
                    ),
                )
                return self.send_json({"ok": True, "id": aid})

            if p == "/api/demo-plan":
                goal = (data.get("goal") or "").strip()
                workspace_id = (data.get("workspace_id") or "").strip()
                ws = one("SELECT * FROM workspaces WHERE id=?", (workspace_id,)) if workspace_id else None
                if not ws:
                    workspace_raw = (data.get("workspace") or "").strip()
                    ws = ensure_workspace(workspace_raw, data.get("workspace_name"))
                    workspace_id = ws["id"]
                if not goal:
                    raise ValueError("Goal gerekli")
                orchestrator_model = data.get("orchestrator_model") or DEFAULT_ORCHESTRATOR
                worker_model = data.get("worker_model") or DEFAULT_WORKER
                orchestrator_effort = data.get("orchestrator_effort") or DEFAULT_ORCHESTRATOR_EFFORT
                worker_effort = data.get("worker_effort") or DEFAULT_WORKER_EFFORT
                orchestrator_tier = data.get("orchestrator_tier") or DEFAULT_ORCHESTRATOR_TIER
                worker_tier = data.get("worker_tier") or DEFAULT_WORKER_TIER
                validate_runtime_config(orchestrator_model, orchestrator_effort, orchestrator_tier, "Orchestrator")
                validate_runtime_config(worker_model, worker_effort, worker_tier, "Worker default")
                max_parallel = max(1, min(int(data.get("max_parallel") or 4), MAX_PARALLEL_HARD))
                pid = str(uuid.uuid4())[:8]
                saved_attachments = []
                for item in (data.get("attachments") or [])[:8]:
                    if isinstance(item, dict) and item.get("data_base64"):
                        saved_attachments.append(save_attachment(pid, item.get("name") or "image.png", item.get("mime") or "image/png", item["data_base64"]))
                attachment_paths = [a["path"] for a in saved_attachments]
                execute(
                    "INSERT INTO plans(id,goal,title,workspace,planner_engine,status,created_at,orchestrator_model,worker_model,max_parallel,mission_dir,usage_start_json,orchestrator_effort,worker_effort,orchestrator_tier,worker_tier,recovery_json,workspace_id,automation_mode,attachments_json,demo_mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, goal, deterministic_mission_title(goal), ws["repo_path"], "demo-simulator", "planning", now(), orchestrator_model, worker_model, max_parallel, str(mission_dir(pid)), "{}", orchestrator_effort, worker_effort, orchestrator_tier, worker_tier, json.dumps(RECOVERY_DEFAULTS), workspace_id, "auto", json.dumps(attachment_paths), 1),
                )
                execute("UPDATE workspaces SET last_opened_at=? WHERE id=?", (now(), workspace_id))
                write_mission_docs(pid)
                threading.Thread(target=build_demo_plan, args=(pid,), daemon=True).start()
                return self.send_json({"ok":True,"plan_id":pid,"status":"planning","demo":True})

            if p == "/api/plan":
                goal = (data.get("goal") or "").strip()
                workspace_id = (data.get("workspace_id") or "").strip()
                ws = one("SELECT * FROM workspaces WHERE id=?", (workspace_id,)) if workspace_id else None
                if not ws:
                    workspace_raw = (data.get("workspace") or "").strip()
                    ws = ensure_workspace(workspace_raw, data.get("workspace_name"))
                    workspace_id = ws["id"]
                workspace = ws["repo_path"]
                if not goal:
                    raise ValueError("Goal gerekli")
                automation_mode = data.get("automation_mode") or "auto"
                if automation_mode not in ("auto", "supervised", "manual"):
                    raise ValueError("Automation mode geçersiz")
                orchestrator_model = data.get("orchestrator_model") or DEFAULT_ORCHESTRATOR
                worker_model = data.get("worker_model") or DEFAULT_WORKER
                orchestrator_effort = data.get("orchestrator_effort") or DEFAULT_ORCHESTRATOR_EFFORT
                worker_effort = data.get("worker_effort") or DEFAULT_WORKER_EFFORT
                orchestrator_tier = data.get("orchestrator_tier") or DEFAULT_ORCHESTRATOR_TIER
                worker_tier = data.get("worker_tier") or DEFAULT_WORKER_TIER
                validate_runtime_config(orchestrator_model, orchestrator_effort, orchestrator_tier, "Orchestrator")
                validate_runtime_config(worker_model, worker_effort, worker_tier, "Worker default")
                max_parallel = max(1, min(int(data.get("max_parallel") or 4), MAX_PARALLEL_HARD))
                recovery = dict(RECOVERY_DEFAULTS)
                requested_recovery = data.get("recovery") or {}
                if isinstance(requested_recovery, dict):
                    for key in RECOVERY_DEFAULTS:
                        if key in requested_recovery:
                            recovery[key] = requested_recovery[key]
                if recovery.get("unknown_local_changes") != "ask":
                    raise ValueError("Unknown local changes policy must be 'ask'")
                if recovery.get("merge_conflicts") not in ("orchestrator", "ask"):
                    raise ValueError("Merge conflict policy invalid")
                if recovery.get("destructive_operations") != "never":
                    raise ValueError("Destructive operations must remain 'never'")
                pid = str(uuid.uuid4())[:8]
                saved_attachments = []
                for item in (data.get("attachments") or [])[:8]:
                    if isinstance(item, dict) and item.get("data_base64"):
                        saved_attachments.append(save_attachment(pid, item.get("name") or "image.png", item.get("mime") or "image/png", item["data_base64"]))
                attachment_paths = [a["path"] for a in saved_attachments]
                execute(
                    "INSERT INTO plans(id,goal,title,workspace,planner_engine,status,created_at,orchestrator_model,worker_model,max_parallel,mission_dir,usage_start_json,orchestrator_effort,worker_effort,orchestrator_tier,worker_tier,recovery_json,workspace_id,automation_mode,attachments_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        pid, goal, deterministic_mission_title(goal), str(Path(workspace).expanduser().resolve()), "codex-chatgpt", "planning", now(),
                        orchestrator_model, worker_model, max_parallel, str(mission_dir(pid)), "{}",
                        orchestrator_effort, worker_effort, orchestrator_tier, worker_tier,
                        json.dumps(recovery, ensure_ascii=False), workspace_id, automation_mode,
                        json.dumps(attachment_paths, ensure_ascii=False),
                    ),
                )
                execute("UPDATE workspaces SET last_opened_at=? WHERE id=?", (now(), workspace_id))
                write_mission_docs(pid)
                threading.Thread(target=build_plan, args=(pid,), daemon=True).start()
                return self.send_json({"ok": True, "plan_id": pid, "status": "planning"})

            if p.startswith("/api/run-plan/"):
                pid = p.split("/")[-1]
                plan=one("SELECT * FROM plans WHERE id=?",(pid,))
                if not plan: raise ValueError('Mission bulunamadı')
                if plan.get('status') not in ('approved','attention','waiting_for_user'):
                    raise ValueError('Planı önce review edip onayla.')
                if not claim_plan_run(pid):
                    return self.send_json({"error": "Bu mission için başka bir işlem zaten çalışıyor."}, 409)
                threading.Thread(target=run_plan, args=(pid,), kwargs={"claimed": True}, daemon=True).start()
                return self.send_json({"ok": True})

            if p.startswith("/api/mission-config/"):
                pid = p.split("/api/mission-config/", 1)[1]
                return self.send_json(mission_config(pid, data))

            if p.startswith("/api/pause-plan/"):
                pid = p.split("/api/pause-plan/", 1)[1]
                return self.send_json(pause_plan(pid, (data.get("reason") or "Paused by user").strip()))

            if p.startswith("/api/resume-plan/"):
                pid = p.split("/api/resume-plan/", 1)[1]
                return self.send_json(resume_plan(pid))

            if p.startswith("/api/reopen-plan/"):
                pid = p.split("/api/reopen-plan/", 1)[1]
                plan = one("SELECT * FROM plans WHERE id=?", (pid,))
                if not plan:
                    raise ValueError("Mission bulunamadı")
                if plan.get("status") != "done":
                    raise ValueError("Yalnızca tamamlanmış mission yeniden açılabilir")
                has_tasks = bool(one("SELECT id FROM tasks WHERE plan_id=? LIMIT 1", (pid,)))
                if not has_tasks:
                    execute(
                        """UPDATE plans SET status='planning',error='',decision='',decision_reason='',
                           evidence_json='[]',questions_json='[]',final_response='',summary='',
                           pending_question_id='',pending_question_json='{}',started_at=NULL,
                           finished_at=NULL WHERE id=?""",
                        (pid,),
                    )
                    log(orchestrator_log_id(pid), "manual", "no-task mission reopened; re-evaluating disposition in the same orchestrator conversation")
                    write_mission_docs(pid)
                    if claim_plan_run(pid):
                        threading.Thread(target=build_plan, args=(pid,), kwargs={"claimed": True}, daemon=True).start()
                    return self.send_json({"ok": True, "status": "planning"})
                execute("UPDATE plans SET status=?,error='',finished_at=NULL WHERE id=?", ("approved", pid))
                log(orchestrator_log_id(pid), "manual", "mission reopened by user; completed tasks remain checkpoints")
                write_mission_docs(pid)
                if claim_plan_run(pid):
                    threading.Thread(target=run_plan, args=(pid,), kwargs={"claimed": True}, daemon=True).start()
                return self.send_json({"ok": True, "status": "resuming"})

            if p.startswith("/api/restart-plan/"):
                pid = p.split("/api/restart-plan/", 1)[1]
                return self.send_json(restart_as_new_mission(pid))

            if p.startswith("/api/pause-task/"):
                tid = p.split("/api/pause-task/", 1)[1]
                return self.send_json(pause_task(tid))

            if p.startswith("/api/resume-task/"):
                tid = p.split("/api/resume-task/", 1)[1]
                return self.send_json(resume_task(tid))

            if p.startswith("/api/reconsider-plan/"):
                pid = p.split("/api/reconsider-plan/", 1)[1]
                mode = (data.get("mode") or "reconsider").strip()
                note = (data.get("note") or "").strip()
                return self.send_json(replan_mission(pid, mode=mode, user_note=note))

            if p.startswith("/api/consultation-answer/"):
                pid = p.split("/api/consultation-answer/", 1)[1]
                consultation_id = (data.get("consultation_id") or "").strip()
                if not consultation_id:
                    raise ValueError("Consultation id gerekli")
                consultation = one(
                    "SELECT task_id FROM consultations WHERE id=? AND plan_id=?",
                    (consultation_id, pid),
                )
                if not consultation:
                    raise ValueError("Consultation bulunamadı")
                saved = []
                for item in (data.get("attachments") or [])[:8]:
                    if isinstance(item, dict) and item.get("data_base64"):
                        saved.append(
                            save_attachment(
                                pid,
                                item.get("name") or "consultation.png",
                                item.get("mime") or "image/png",
                                item["data_base64"],
                                task_id=consultation["task_id"],
                            )
                        )
                answer = (data.get("answer") or "").strip()
                option = (data.get("option") or "").strip()
                if option:
                    answer = f"{option}\n{answer}".strip()
                return self.send_json(
                    answer_consultation(pid, consultation_id, answer, [x["path"] for x in saved])
                )

            if p.startswith("/api/reconstruct-orchestrator/"):
                pid = p.split("/api/reconstruct-orchestrator/", 1)[1]
                plan = one("SELECT * FROM plans WHERE id=?", (pid,))
                if not plan:
                    raise ValueError("Mission bulunamadı")
                if not claim_plan_run(pid):
                    return self.send_json({"error": "Bu mission için başka bir işlem zaten çalışıyor."}, 409)
                threading.Thread(
                    target=lambda: _run_reconstruct_and_release(pid),
                    daemon=True,
                ).start()
                return self.send_json({"ok": True, "status": "reconstructing"})

            if p.startswith("/api/preflight-action/"):
                pid = p.split("/api/preflight-action/", 1)[1]
                return self.send_json(apply_preflight_action(pid, data.get("action"), data.get("paths") or []))

            if p.startswith("/api/run-task/"):
                return self.send_json({"error": "Direct task execution is disabled; start the approved mission instead."}, 410)

            if p.startswith("/api/apply-plan/"):
                pid = p.split("/api/apply-plan/", 1)[1]
                result = apply_plan(pid)
                return self.send_json(result)

            if p.startswith("/api/follow-up/"):
                tid = p.split("/api/follow-up/",1)[1]
                prompt = (data.get("prompt") or "").strip()
                if not prompt and not data.get("attachments"):
                    raise ValueError("Mesaj veya attachment gerekli")
                task = one("SELECT * FROM tasks WHERE id=?", (tid,))
                if not task:
                    raise ValueError("Task bulunamadı")
                image_paths=[]
                for item in (data.get("attachments") or [])[:8]:
                    if isinstance(item, dict) and item.get("data_base64"):
                        saved=save_attachment(task["plan_id"], item.get("name") or "image.png", item.get("mime") or "image/png", item["data_base64"], task_id=tid)
                        image_paths.append(saved["path"])
                mid=str(uuid.uuid4())[:10]
                status = manual_followup_delivery_status(tid)
                with APP_SERVER_CONTROLS_LOCK:
                    app_server_active = tid in APP_SERVER_CONTROLS
                execute("INSERT INTO task_messages(id,task_id,plan_id,ts,text,attachments_json,status) VALUES(?,?,?,?,?,?,?)", (mid,tid,task['plan_id'],now(),prompt,json.dumps(image_paths),status))
                if status=='sending':
                    if app_server_active:
                        if not steer_app_server(tid, mid, prompt, image_paths):
                            # The active turn may have completed between the
                            # availability check and the steer request. The
                            # helper records that race as a failed message so
                            # it is visible instead of being silently lost.
                            pass
                    else:
                        threading.Thread(target=run_manual_followup_message,args=(mid,),daemon=True).start()
                else:
                    log(tid,'manual','user message queued for the next Codex turn')
                return self.send_json({"ok":True,"queued":status=='queued',"message_id":mid})

            if p.startswith("/api/orchestrator-follow-up/"):
                pid=p.split("/api/orchestrator-follow-up/",1)[1]
                prompt=(data.get('prompt') or '').strip()
                if not prompt:
                    raise ValueError('Mesaj gerekli')
                threading.Thread(target=run_orchestrator_followup,args=(pid,prompt,[]),daemon=True).start()
                return self.send_json({'ok':True})

            if p.startswith("/api/task-config/"):
                tid=p.split("/api/task-config/",1)[1]
                task=one("SELECT * FROM tasks WHERE id=?",(tid,))
                if not task: raise ValueError('Task bulunamadı')
                plan=one("SELECT * FROM plans WHERE id=?",(task['plan_id'],))
                if plan.get('status') not in ('planned','approved'):
                    raise ValueError('Task assignment yalnızca execution başlamadan önce değiştirilebilir.')
                agent_id=(data.get('agent_id') or task.get('agent_id') or '').strip()
                if agent_id and not one("SELECT id FROM agents WHERE id=?",(agent_id,)):
                    raise ValueError('Agent profile bulunamadı')
                mode=data.get('mode') or task.get('mode') or 'read'
                if mode not in ('read','write'): raise ValueError('Geçersiz task mode')
                execute("UPDATE tasks SET agent_id=?,mode=? WHERE id=?",(agent_id,mode,tid))
                if plan.get('status')=='approved':
                    execute("UPDATE plans SET status=?,approved_at=NULL WHERE id=?",('planned',plan['id']))
                write_mission_docs(plan['id'])
                return self.send_json({'ok':True})

            if p.startswith("/api/approve-plan/"):
                pid=p.split("/api/approve-plan/",1)[1]
                plan=one("SELECT * FROM plans WHERE id=?",(pid,))
                if not plan: raise ValueError('Mission bulunamadı')
                if plan.get('status')!='planned': raise ValueError('Plan review için hazır değil')
                tasks=rows("SELECT * FROM tasks WHERE plan_id=? ORDER BY seq",(pid,))
                if not tasks: raise ValueError('Onaylanacak task yok')
                missing=[t['title'] for t in tasks if not t.get('agent_id')]
                if missing: raise ValueError('Agent atanmamış task var: '+', '.join(missing))
                execute("UPDATE plans SET status=?,approved_at=?,approval_note=? WHERE id=?",('approved',now(),(data.get('note') or '').strip(),pid))
                log(orchestrator_log_id(pid),'supervisor','plan approved by user · execution is still paused until Start mission')
                write_mission_docs(pid)
                return self.send_json({'ok':True})

            if p.startswith("/api/open-terminal/"):
                tid=p.split("/api/open-terminal/",1)[1]
                task=one("SELECT * FROM tasks WHERE id=?",(tid,))
                if not task: raise ValueError('Task bulunamadı')
                plan=one("SELECT * FROM plans WHERE id=?",(task['plan_id'],))
                open_terminal_at(task.get('workspace') or plan.get('workspace'))
                return self.send_json({'ok':True})

            if p.startswith("/api/cancel-task/"):
                tid = p.split("/")[-1]
                task=one("SELECT * FROM tasks WHERE id=?",(tid,))
                if not task: raise ValueError('Task bulunamadı')
                plan=one("SELECT * FROM plans WHERE id=?",(task['plan_id'],))
                with RUNNERS_LOCK:
                    proc = RUNNERS.get(tid)
                if proc:
                    terminate_process(proc)
                    execute("UPDATE tasks SET status=?, error=?, finished_at=? WHERE id=?", ("cancelled", "User cancelled", now(), tid))
                    return self.send_json({"ok": True})
                if plan and int(plan.get('demo_mode') or 0) and task.get('status')=='running':
                    execute("UPDATE tasks SET status=?,error=?,finished_at=? WHERE id=?",('cancelled','User cancelled demo agent',now(),tid))
                    return self.send_json({'ok':True})
                return self.send_json({"ok": False, "message": "Task çalışmıyor"}, 409)

            return self.send_json({"error": "not found"}, 404)
        except Exception as e:
            return self.send_json({"error": str(e)}, 400)
