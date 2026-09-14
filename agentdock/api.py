"""HTTP adapter for AgentDock services.

Routes in this module only parse requests, call an owning service, and
serialize its result.  Persistence, Git, Codex transport, and lifecycle
decisions live in the domain modules.
"""

import json
from http.server import SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config
from .mission import (
    approve_plan,
    apply_plan,
    create_agent_profile,
    create_plan_request,
    create_workspace_request,
    docs_payload,
    health_payload,
    live_payload,
    mission_config,
    plan_diff_payload,
    pause_plan,
    quota_payload,
    replan_mission,
    reopen_plan,
    restart_as_new_mission,
    resume_plan,
    pause_task,
    resume_task,
    start_plan,
    state_payload,
    workspace_browse,
)
from .orchestrator import (
    answer_consultation_request,
    start_reconstruct,
    start_orchestrator_followup,
)
from .preflight import apply_preflight_action
from .tasks import cancel_task, configure_task, open_task_terminal, send_task_followup
from .timeline import (
    diff_payload,
    events_payload,
    file_preview_payload,
    logs_payload,
    messages_payload,
    task_files_payload,
    timeline_for,
)
class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        relative = urlparse(path).path.lstrip("/") or "index.html"
        static_root = config.STATIC.resolve()
        candidate = (static_root / relative).resolve()
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
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            # Browser polling/navigation may close a request after the payload
            # was prepared. That is a normal client disconnect, not a server
            # failure worth printing as an exception traceback.
            return

    def body(self):
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def _service_result(self, result):
        status = 409 if isinstance(result, dict) and result.get("status") == "busy" else 200
        return self.send_json(result, status)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/health":
            return self.send_json(health_payload())
        if path == "/api/state":
            return self.send_json(state_payload())
        if path.startswith("/api/live/"):
            try:
                return self.send_json(live_payload(path.rsplit("/", 1)[-1]))
            except KeyError as exc:
                return self.send_json({"error": str(exc.args[0])}, 404)
        if path.startswith("/api/logs/"):
            return self.send_json(logs_payload(path.split("/api/logs/", 1)[1]))
        if path.startswith("/api/events/"):
            return self.send_json(events_payload(path.split("/api/events/", 1)[1]))
        if path.startswith("/api/messages/"):
            return self.send_json(messages_payload(path.split("/api/messages/", 1)[1]))
        if path.startswith("/api/timeline/"):
            return self.send_json(timeline_for(path.split("/api/timeline/", 1)[1]))
        if path.startswith("/api/diff/"):
            try:
                return self.send_json(diff_payload(path.split("/api/diff/", 1)[1]))
            except KeyError as exc:
                return self.send_json({"error": str(exc.args[0])}, 404)
            except Exception as exc:
                return self.send_json({"error": str(exc)}, 409)
        if path.startswith("/api/task-files/"):
            try:
                return self.send_json(task_files_payload(path.rsplit("/", 1)[-1]))
            except KeyError as exc:
                return self.send_json({"error": str(exc.args[0])}, 404)
        if path.startswith("/api/file-preview/"):
            try:
                query = parse_qs(urlparse(self.path).query)
                return self.send_json(file_preview_payload(
                    path.rsplit("/", 1)[-1],
                    (query.get("path") or [""])[0],
                    (query.get("task") or [""])[0],
                ))
            except KeyError as exc:
                return self.send_json({"error": str(exc.args[0])}, 404)
            except ValueError as exc:
                return self.send_json({"error": str(exc)}, 400)
        if path.startswith("/api/plan-diff/"):
            try:
                return self.send_json(plan_diff_payload(path.split("/api/plan-diff/", 1)[1]))
            except KeyError as exc:
                return self.send_json({"error": str(exc.args[0])}, 404)
            except Exception as exc:
                return self.send_json({"error": str(exc)}, 409)
        if path == "/api/workspace/browse":
            return self.send_json(workspace_browse())
        if path == "/api/quota":
            return self.send_json(quota_payload(force=True, wait=True))
        if path.startswith("/api/docs/"):
            return self.send_json(docs_payload(path.rsplit("/", 1)[-1]))
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            data = self.body()
            if path == "/api/workspaces":
                workspace = create_workspace_request(data)
                return self.send_json({"ok": True, "workspace": workspace})
            if path == "/api/agents":
                return self.send_json(create_agent_profile(data))
            if path == "/api/demo-plan":
                return self.send_json(create_plan_request(data, demo=True))
            if path == "/api/plan":
                return self.send_json(create_plan_request(data))
            if path.startswith("/api/run-plan/"):
                return self._service_result(start_plan(path.rsplit("/", 1)[-1]))
            if path.startswith("/api/mission-config/"):
                return self.send_json(mission_config(path.split("/api/mission-config/", 1)[1], data))
            if path.startswith("/api/pause-plan/"):
                return self.send_json(pause_plan(path.split("/api/pause-plan/", 1)[1], (data.get("reason") or "Paused by user").strip()))
            if path.startswith("/api/resume-plan/"):
                return self.send_json(resume_plan(path.split("/api/resume-plan/", 1)[1]))
            if path.startswith("/api/reopen-plan/"):
                return self._service_result(reopen_plan(path.split("/api/reopen-plan/", 1)[1]))
            if path.startswith("/api/restart-plan/"):
                return self.send_json(restart_as_new_mission(path.split("/api/restart-plan/", 1)[1]))
            if path.startswith("/api/pause-task/"):
                return self.send_json(pause_task(path.split("/api/pause-task/", 1)[1]))
            if path.startswith("/api/resume-task/"):
                return self.send_json(resume_task(path.split("/api/resume-task/", 1)[1]))
            if path.startswith("/api/reconsider-plan/"):
                return self.send_json(
                    replan_mission(
                        path.split("/api/reconsider-plan/", 1)[1],
                        mode=(data.get("mode") or "reconsider").strip(),
                        user_note=(data.get("note") or "").strip(),
                    )
                )
            if path.startswith("/api/consultation-answer/"):
                return self.send_json(answer_consultation_request(path.split("/api/consultation-answer/", 1)[1], data))
            if path.startswith("/api/reconstruct-orchestrator/"):
                return self._service_result(start_reconstruct(path.split("/api/reconstruct-orchestrator/", 1)[1]))
            if path.startswith("/api/preflight-action/"):
                return self.send_json(
                    apply_preflight_action(
                        path.split("/api/preflight-action/", 1)[1],
                        data.get("action"),
                        data.get("paths") or [],
                    )
                )
            if path.startswith("/api/run-task/"):
                return self.send_json(
                    {"error": "Direct task execution is disabled; start the approved mission instead."},
                    410,
                )
            if path.startswith("/api/apply-plan/"):
                return self.send_json(apply_plan(path.split("/api/apply-plan/", 1)[1]))
            if path.startswith("/api/follow-up/"):
                return self.send_json(
                    send_task_followup(
                        path.split("/api/follow-up/", 1)[1],
                        data.get("prompt") or "",
                        data.get("attachments") or [],
                    )
                )
            if path.startswith("/api/orchestrator-follow-up/"):
                prompt = (data.get("prompt") or "").strip()
                return self.send_json(
                    start_orchestrator_followup(
                        path.split("/api/orchestrator-follow-up/", 1)[1], prompt, data.get("attachments") or []
                    )
                )
            if path.startswith("/api/task-config/"):
                return self.send_json(configure_task(path.split("/api/task-config/", 1)[1], data))
            if path.startswith("/api/approve-plan/"):
                return self.send_json(approve_plan(path.split("/api/approve-plan/", 1)[1], data.get("note") or ""))
            if path.startswith("/api/open-terminal/"):
                return self.send_json(open_task_terminal(path.split("/api/open-terminal/", 1)[1]))
            if path.startswith("/api/cancel-task/"):
                return self.send_json(cancel_task(path.rsplit("/", 1)[-1]))
            return self.send_json({"error": "not found"}, 404)
        except Exception as exc:
            return self.send_json({"error": str(exc)}, 400)
