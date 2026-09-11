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

def validate_runtime_config(model, effort, tier, label):
    allowed = None
    if model:
        for item in discover_model_catalog():
            if item.get("slug") == model:
                allowed = set(item.get("reasoning_levels") or [])
                break
    if allowed is None:
        allowed = MODEL_EFFORTS.get(model)
    if allowed and effort not in allowed:
        raise ValueError(f"{label}: {model} için reasoning effort '{effort}' geçerli değil")
    if tier not in VALID_TIERS:
        raise ValueError(f"{label}: speed/service tier '{tier}' geçerli değil")

def terminate_process(proc):
    """Terminate a Codex process and its shell descendants on macOS/Linux."""
    if not proc or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except Exception:
            pass

def discover_model_catalog(force=False):
    """Read the bundled Codex model catalog without making a model call."""
    with MODEL_CATALOG_LOCK:
        cached = MODEL_CATALOG_CACHE.get("value") or []
        if cached and not force and (time.time() - MODEL_CATALOG_CACHE.get("ts", 0)) < 300:
            return list(cached)
    exe = shutil.which("codex")
    if not exe:
        return []
    try:
        result = shell([exe, "debug", "models", "--bundled"], check=False, timeout=10)
        payload = json.loads(result.stdout or "{}")
        catalog = []
        for item in payload.get("models") or []:
            if not isinstance(item, dict) or not item.get("slug"):
                continue
            catalog.append({
                "slug": item["slug"],
                "display_name": item.get("display_name") or item["slug"],
                "reasoning_levels": [
                    x.get("effort") for x in (item.get("supported_reasoning_levels") or [])
                    if isinstance(x, dict) and x.get("effort")
                ],
                "speed_tiers": [
                    x for x in (item.get("additional_speed_tiers") or []) if isinstance(x, str)
                ],
            })
    except Exception:
        catalog = []
    with MODEL_CATALOG_LOCK:
        MODEL_CATALOG_CACHE["ts"] = time.time()
        MODEL_CATALOG_CACHE["value"] = catalog
    return list(catalog)

def engine_status(force=False):
    # Live dashboard polls often; avoid spawning `codex` on every request.
    with ENGINE_STATUS_LOCK:
        cached = ENGINE_STATUS_CACHE.get("value")
        if cached is not None and not force and (time.time() - ENGINE_STATUS_CACHE.get("ts", 0)) < 12:
            return cached

    codex = shutil.which("codex")
    out = {
        "codex": {"installed": bool(codex), "path": codex, "version": "", "login": "unknown", "models": [], "app_server": bool(codex), "transport": CODEX_TRANSPORT},
        "git": {"installed": bool(shutil.which("git")), "path": shutil.which("git")},
    }
    if codex:
        try:
            out["codex"]["version"] = shell([codex, "--version"], check=False, timeout=3).stdout.strip()
        except Exception:
            pass
        out["codex"]["models"] = discover_model_catalog()
        try:
            r = shell([codex, "login", "status"], check=False, timeout=4)
            text = (r.stdout + "\n" + r.stderr).strip()
            if r.returncode == 0:
                out["codex"]["login"] = text or "signed in"
            elif text:
                out["codex"]["login"] = text[:300]
        except Exception:
            pass

    with ENGINE_STATUS_LOCK:
        ENGINE_STATUS_CACHE["ts"] = time.time()
        ENGINE_STATUS_CACHE["value"] = out
    return out

def _app_server_request(method, params=None, timeout=10):
    exe = shutil.which("codex")
    if not exe:
        raise RuntimeError("codex CLI PATH içinde bulunamadı")
    proc = subprocess.Popen(
        [exe, "app-server", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    stderr_buf = []
    def drain_stderr():
        try:
            for line in iter(proc.stderr.readline, ""):
                stderr_buf.append(line)
        except Exception:
            pass
    threading.Thread(target=drain_stderr, daemon=True).start()

    def send(obj):
        proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        proc.stdin.flush()

    def read_until(request_id, deadline):
        while time.time() < deadline:
            remaining = max(0.05, deadline - time.time())
            try:
                ready, _, _ = select.select([proc.stdout], [], [], remaining)
            except Exception:
                ready = [proc.stdout]
            if not ready:
                continue
            line = proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("id") == request_id:
                if "error" in msg:
                    raise RuntimeError(str(msg["error"]))
                return msg.get("result")
        detail = "".join(stderr_buf)[-2000:]
        raise RuntimeError(f"Codex app-server timeout for {method}. {detail}".strip())

    try:
        deadline = time.time() + timeout
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "agentdock", "title": "AgentDock", "version": "0.4.0"}, "capabilities": {"experimentalApi": True}}})
        read_until(1, deadline)
        send({"method": "initialized"})
        send({"id": 2, "method": method, **({"params": params} if params is not None else {})})
        return read_until(2, deadline)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=1)
        except Exception:
            try: proc.kill()
            except Exception: pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                stream.close()
            except Exception:
                pass

def _normalize_rate_limits(result):
    result = result or {}
    by_id = result.get("rateLimitsByLimitId") or {}
    snap = by_id.get("codex") or result.get("rateLimits") or {}
    windows = {}
    for key in ("primary", "secondary"):
        bucket = snap.get(key)
        if not isinstance(bucket, dict):
            continue
        mins = bucket.get("windowDurationMins")
        if mins is None:
            continue
        item = {
            "used_percent": bucket.get("usedPercent"),
            "remaining_percent": None if bucket.get("usedPercent") is None else max(0, 100 - float(bucket.get("usedPercent"))),
            "window_minutes": mins,
            "resets_at": bucket.get("resetsAt"),
        }
        windows[str(mins)] = item
    return {
        "status": "ok",
        "available": True,
        "plan_type": snap.get("planType"),
        "five_hour": windows.get("300"),
        "weekly": windows.get("10080"),
        "windows": windows,
        "fetched_at": now(),
    }

def _refresh_quota_cache():
    try:
        value = _normalize_rate_limits(_app_server_request("account/rateLimits/read"))
    except Exception as e:
        value = {"status": "error", "available": False, "error": str(e), "fetched_at": now()}
    with QUOTA_LOCK:
        QUOTA_CACHE["ts"] = time.time()
        QUOTA_CACHE["value"] = value
        QUOTA_CACHE["refreshing"] = False

def quota_status(force=False, wait=False):
    with QUOTA_LOCK:
        fresh = (time.time() - QUOTA_CACHE.get("ts", 0)) < 45
        if fresh and not force:
            return dict(QUOTA_CACHE["value"])
        if not QUOTA_CACHE.get("refreshing"):
            QUOTA_CACHE["refreshing"] = True
            t = threading.Thread(target=_refresh_quota_cache, daemon=True)
            t.start()
        else:
            t = None
        current = dict(QUOTA_CACHE.get("value") or {})
        current["refreshing"] = True
    if wait:
        deadline = time.time() + 12
        while time.time() < deadline:
            with QUOTA_LOCK:
                if not QUOTA_CACHE.get("refreshing"):
                    return dict(QUOTA_CACHE["value"])
            time.sleep(0.05)
    return current

def consultation_attachment_paths(task_id):
    """Return still-present image attachments supplied for a task consultation."""
    if not task_id:
        return []
    paths = []
    consultation_rows = rows(
        "SELECT user_answer_json FROM consultations WHERE task_id=? ORDER BY created_at,id",
        (task_id,),
    )
    for item in consultation_rows:
        answer = safe_json(item.get("user_answer_json"), {})
        for raw in answer.get("attachments") or [] if isinstance(answer, dict) else []:
            path = Path(str(raw)).expanduser().resolve()
            if path.is_file() and str(path) not in paths:
                paths.append(str(path))
    return paths[:8]

def task_input_attachment_paths(plan, task):
    paths = []
    for raw in plan_attachment_paths(plan) + consultation_attachment_paths((task or {}).get("id")):
        value = str(Path(raw).expanduser().resolve())
        if value not in paths and Path(value).is_file():
            paths.append(value)
    return paths[:16]

def parse_codex_final(stdout):
    final = []

    def normalize(value):
        if isinstance(value, str):
            return value
        if value is None:
            return ""
        return json.dumps(value, ensure_ascii=False)

    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        params = obj.get("params") if isinstance(obj.get("params"), dict) else {}
        item = obj.get("item") or params.get("item") or {}
        item_type = str(item.get("type") or "").replace("-", "_")
        if obj.get("type") == "item.completed" and item_type in ("agent_message", "agentMessage"):
            text = normalize(item.get("text") or item.get("message"))
            if text:
                final.append(text)
        if obj.get("method") == "item/completed" and item_type in ("agentMessage", "agent_message"):
            text = normalize(item.get("text") or item.get("message"))
            if text:
                final.append(text)
        if obj.get("type") in ("message.completed", "response.completed"):
            text = obj.get("text") or obj.get("message") or ""
            if isinstance(text, str) and text:
                final.append(text)
    return final[-1] if final else stdout[-16000:]

def pretty_codex_event(line):
    raw = line.rstrip("\n")
    try:
        obj = json.loads(raw)
    except Exception:
        return raw
    typ = obj.get("type") or obj.get("method") or "event"
    params = obj.get("params") if isinstance(obj.get("params"), dict) else {}
    item = obj.get("item") or params.get("item") or {}
    itype = str(item.get("type") or "")
    normalized_type = re.sub(r"(?<!^)(?=[A-Z])", "_", itype).replace("-", "_").lower()
    if normalized_type in ("agent_message", "message"):
        text = item.get("text") or item.get("message") or ""
        try:
            parsed = json.loads(text) if isinstance(text, str) else None
        except Exception:
            parsed = None
        if isinstance(parsed, dict) and ("decision" in parsed or "tasks" in parsed):
            return "planner response captured · structured disposition parsed"
        return f"agent: {text}" if text else typ
    if normalized_type in ("command_execution", "command"):
        cmd = item.get("command") or item.get("cmd") or ""
        status = item.get("status") or ""
        return f"cmd {status}: {cmd}".strip()
    if normalized_type in ("reasoning", "analysis"):
        summary = item.get("summary")
        if isinstance(summary, list):
            summary_parts = []
            for part in summary:
                if isinstance(part, str):
                    summary_parts.append(part)
                elif isinstance(part, dict):
                    value = part.get("text") or part.get("summary") or part.get("content") or ""
                    if value:
                        summary_parts.append(str(value))
            summary = " ".join(summary_parts)
        text = item.get("text") or summary or ""
        return f"thinking: {text}" if text else typ
    if normalized_type == "file_change":
        changes = item.get("changes") or []
        detail = ", ".join(f"{c.get('kind','?')} {c.get('path','')}" for c in changes[:8])
        return f"files {item.get('status','')}: {detail}".strip()
    if normalized_type == "todo_list":
        todos = item.get("items") or []
        done = sum(1 for x in todos if x.get("completed"))
        return f"todo: {done}/{len(todos)} complete"
    if normalized_type == "web_search":
        return f"web search: {item.get('query','')}"
    if normalized_type in ("mcp_tool_call", "collab_tool_call"):
        return f"tool {item.get('status','')}: {item.get('tool') or item.get('server') or itype}"
    text = obj.get("text") or obj.get("message") or ""
    if isinstance(text, str) and text:
        return f"{typ}: {text}"
    if item:
        label = item.get("name") or item.get("title") or itype
        return f"{typ}: {label}" if label else typ
    return typ

def infer_plan_id(task_id):
    if not task_id:
        return ""
    if str(task_id).startswith("orchestrator:"):
        return str(task_id).split(":", 1)[1]
    task = one("SELECT plan_id FROM tasks WHERE id=?", (task_id,))
    return task.get("plan_id", "") if task else ""

def _app_server_sandbox(workspace, mode):
    if mode == "write":
        return {"type": "workspaceWrite", "writableRoots": [str(workspace)]}
    return {"type": "readOnly", "access": {"type": "fullAccess"}}

def app_server_input_items(prompt, images=None, task_id=None):
    items = []
    if str(prompt or "").strip():
        items.append({"type": "text", "text": str(prompt)})
    for raw in images or []:
        path = Path(str(raw)).expanduser().resolve()
        if path.is_file():
            items.append({"type": "localImage", "path": str(path)})
        else:
            log(task_id, "supervisor", f"App Server image attachment skipped because it no longer exists: {path}")
    return items or [{"type": "text", "text": "Continue the assigned task."}]

def run_codex_app_server(prompt, workspace, mode="read", model="", task_id=None, reasoning_effort="", service_tier="default",
                         images=None, resume_thread_id="", session_kind="worker", output_schema=""):
    """Run one Codex turn through the line-delimited App Server protocol.

    The client is intentionally short-lived per turn. This gives AgentDock the
    App Server event vocabulary and resumable thread ids without introducing a
    daemon lifecycle dependency; resume_thread_id keeps the conversation
    continuous. The legacy exec transport remains available as the default.
    """
    workspace = str(Path(workspace).expanduser().resolve())
    if not Path(workspace).is_dir():
        raise RuntimeError(f"Workspace bulunamadı: {workspace}")
    exe = shutil.which("codex")
    if not exe:
        raise RuntimeError("codex CLI PATH içinde bulunamadı")
    plan_id = infer_plan_id(task_id)
    session_id = create_agent_session(
        plan_id, task_id or "", session_kind, model, reasoning_effort,
        service_tier or "default", mode, workspace,
    )
    proc = subprocess.Popen(
        [exe, "app-server", "--stdio"],
        cwd=workspace,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    with RUNNERS_LOCK:
        if task_id:
            RUNNERS[task_id] = proc
    stderr_buf = []
    messages = []
    stdout_queue = queue.Queue()
    send_lock = threading.Lock()
    deferred_messages = []

    def drain_stderr():
        try:
            for line in iter(proc.stderr.readline, ""):
                stderr_buf.append(line)
                log(task_id, "stderr", line.rstrip("\n"))
        except Exception:
            pass

    threading.Thread(target=drain_stderr, daemon=True).start()

    def drain_stdout():
        try:
            for line in iter(proc.stdout.readline, ""):
                stdout_queue.put(("line", line))
        finally:
            stdout_queue.put(("eof", ""))

    # Reading App Server stdout on its own thread avoids a subtle interaction
    # between select() and TextIOWrapper's user-space read buffer: one read can
    # contain several JSON messages, while the file descriptor then appears
    # non-readable even though another complete line is already buffered.
    threading.Thread(target=drain_stdout, daemon=True).start()

    def send(obj):
        with send_lock:
            if proc.poll() is not None:
                raise RuntimeError("Codex App Server process is no longer running")
            proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
            proc.stdin.flush()

    def read_message(deadline):
        while time.time() < deadline:
            remaining = max(0.05, deadline - time.time())
            try:
                kind, line = stdout_queue.get(timeout=remaining)
            except queue.Empty:
                break
            if kind == "eof":
                break
            try:
                return json.loads(line)
            except Exception:
                log(task_id, "stdout", line.rstrip("\n"))
        detail = "".join(stderr_buf)[-2000:]
        raise RuntimeError(f"Codex app-server bağlantısı sonlandı veya zaman aşımına uğradı. {detail}".strip())

    def persist(message):
        messages.append(message)
        raw = json.dumps(message, ensure_ascii=False)
        record_codex_event(session_id, task_id or "", plan_id, raw)
        log(task_id, "stdout", pretty_codex_event(raw))

    def answer_server_request(message):
        method = str(message.get("method") or "")
        if not message.get("id") or not method:
            return
        if "requestApproval" in method:
            # AgentDock's controlled worker path never auto-approves an
            # unexpected server approval request. The user can steer/retry.
            send({"id": message["id"], "result": {"decision": "decline"}})
            log(task_id, "supervisor", "App Server approval request declined by AgentDock safety policy")

    def request(request_id, method, params):
        send({"id": request_id, "method": method, "params": params})
        deadline = time.time() + 45
        while time.time() < deadline:
            message = read_message(deadline)
            persist(message)
            if message.get("method"):
                answer_server_request(message)
            if message.get("id") == request_id:
                if message.get("error"):
                    raise RuntimeError(str(message["error"]))
                return message.get("result") or {}
            if message.get("method"):
                # JSON-RPC notifications are allowed to arrive before the
                # response to a request. Keep them for the turn event loop
                # instead of losing a fast completion notification.
                deferred_messages.append(message)
        raise RuntimeError(f"Codex app-server {method} isteği zaman aşımına uğradı")

    def next_event(deadline):
        if deferred_messages:
            return deferred_messages.pop(0)
        return read_message(deadline)

    final = ""
    completed_status = "failed"
    try:
        request(1, "initialize", {
            "clientInfo": {"name": "agentdock", "title": "AgentDock", "version": "0.12.0"},
            "capabilities": {"experimentalApi": True},
        })
        send({"method": "initialized", "params": {}})
        thread_params = {
            "model": model or DEFAULT_WORKER,
            "cwd": workspace,
            "serviceName": "agentdock",
            "serviceTier": service_tier or "default",
        }
        if resume_thread_id:
            thread_result = request(2, "thread/resume", {"threadId": resume_thread_id})
        else:
            thread_result = request(2, "thread/start", thread_params)
        thread = thread_result.get("thread") if isinstance(thread_result, dict) else {}
        thread_id = str((thread or {}).get("id") or resume_thread_id or "")
        if thread_id:
            execute("UPDATE agent_sessions SET thread_id=? WHERE id=?", (thread_id, session_id))
            if plan_id and session_kind.startswith("orchestrator"):
                execute(
                    "UPDATE plans SET orchestrator_thread_id=? WHERE id=? AND (orchestrator_thread_id='' OR orchestrator_thread_id=?)",
                    (thread_id, plan_id, thread_id),
                )

        turn_params = {
            "threadId": thread_id,
            "input": app_server_input_items(prompt, images, task_id),
            "cwd": workspace,
            "model": model or DEFAULT_WORKER,
            "effort": reasoning_effort or DEFAULT_WORKER_EFFORT,
            "serviceTier": service_tier or "default",
            "approvalPolicy": "never",
            "sandboxPolicy": _app_server_sandbox(workspace, mode),
            "summary": "concise",
        }
        if output_schema:
            schema_path = Path(output_schema).expanduser().resolve()
            if not schema_path.is_file():
                raise RuntimeError(f"Codex output schema bulunamadı: {schema_path}")
            turn_params["outputSchema"] = json.loads(schema_path.read_text())
        turn_result = request(3, "turn/start", turn_params)
        turn = turn_result.get("turn") if isinstance(turn_result, dict) else {}
        turn_id = str((turn or {}).get("id") or "")
        if turn_id:
            execute("UPDATE agent_sessions SET turn_id=? WHERE id=?", (turn_id, session_id))
        if task_id and thread_id and turn_id:
            with APP_SERVER_CONTROLS_LOCK:
                APP_SERVER_CONTROLS[task_id] = {
                    "send": send,
                    "interrupt": lambda: send({
                        "method": "turn/interrupt",
                        "params": {"threadId": thread_id, "turnId": turn_id},
                    }),
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "next_request_id": 1000,
                    "pending": {},
                }
        deadline = time.time() + 900
        while time.time() < deadline:
            message = next_event(deadline)
            persist(message)
            if message.get("method"):
                answer_server_request(message)
            if message.get("id"):
                with APP_SERVER_CONTROLS_LOCK:
                    control = APP_SERVER_CONTROLS.get(task_id) if task_id else None
                    message_id = control.get("pending", {}).pop(message["id"], None) if control else None
                if message_id:
                    if message.get("error"):
                        error = str(message["error"])
                        execute("UPDATE task_messages SET status=?,error=? WHERE id=?", ("failed", error, message_id))
                        log(task_id, "manual", f"App Server steer failed: {error}")
                    else:
                        execute("UPDATE task_messages SET status=?,error=? WHERE id=?", ("delivered", "", message_id))
                        log(task_id, "manual", "App Server steer delivered to the active turn")
            method = message.get("method") or ""
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            if method in ("turn/completed", "turn/failed"):
                event_turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                event_turn_id = str(params.get("turnId") or event_turn.get("id") or "")
                if not turn_id or not event_turn_id or event_turn_id == turn_id:
                    completed_status = str(event_turn.get("status") or params.get("status") or ("failed" if method == "turn/failed" else "completed"))
                    break
        else:
            raise RuntimeError("Codex app-server turn zaman aşımına uğradı")

        if completed_status not in ("completed", "complete", "success"):
            raise RuntimeError(f"Codex turn tamamlanmadı: {completed_status}")
        final = parse_codex_final("\n".join(json.dumps(x, ensure_ascii=False) for x in messages))
        if not final:
            final = "App Server turn completed without an agent message."
        finish_agent_session(session_id, "completed", final)
        return final
    except Exception as exc:
        current_status = (one("SELECT status FROM tasks WHERE id=?", (task_id,)) or {}).get("status") if task_id else ""
        stopped = current_status in ("cancelled", "paused_by_user", "pausing") or plan_is_paused(plan_id)
        finish_agent_session(session_id, "cancelled" if stopped else "failed", str(exc))
        raise
    finally:
        with APP_SERVER_CONTROLS_LOCK:
            control = APP_SERVER_CONTROLS.pop(task_id, None) if task_id else None
            pending_message_ids = list((control or {}).get("pending", {}).values())
        for message_id in pending_message_ids:
            execute("UPDATE task_messages SET status=?,error=? WHERE id=? AND status=?", ("failed", "App Server turn ended before this message was delivered", message_id, "sending"))
        with RUNNERS_LOCK:
            if task_id:
                RUNNERS.pop(task_id, None)
        try:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=1)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                stream.close()
            except Exception:
                pass

def run_codex(prompt, workspace, mode="read", model="", task_id=None, reasoning_effort="", service_tier="default",
              images=None, resume_thread_id="", session_kind="worker", output_schema=""):
    if CODEX_TRANSPORT in ("app-server", "app_server"):
        return run_codex_app_server(prompt, workspace, mode, model, task_id, reasoning_effort, service_tier,
                                    images=images, resume_thread_id=resume_thread_id, session_kind=session_kind,
                                    output_schema=output_schema)
    workspace = str(Path(workspace).expanduser().resolve())
    if not Path(workspace).is_dir():
        raise RuntimeError(f"Workspace bulunamadı: {workspace}")
    exe = shutil.which("codex")
    if not exe:
        raise RuntimeError("codex CLI PATH içinde bulunamadı")
    plan_id = infer_plan_id(task_id)
    session_id = create_agent_session(plan_id, task_id or "", session_kind, model, reasoning_effort, service_tier, mode, workspace)
    if resume_thread_id:
        # The CLI's resume stream may not repeat a thread.started event. Keep
        # the new turn record bound to the known conversation immediately.
        execute("UPDATE agent_sessions SET thread_id=? WHERE id=?", (resume_thread_id, session_id))
    args = [
        exe,
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--sandbox",
        "workspace-write" if mode == "write" else "read-only",
        "--cd",
        workspace,
    ]
    if model:
        args += ["--model", model]
    if reasoning_effort:
        args += ["-c", f'model_reasoning_effort="{reasoning_effort}"']
    if service_tier:
        args += ["-c", f'service_tier="{service_tier}"']
    if output_schema:
        schema_path = Path(output_schema).expanduser().resolve()
        if not schema_path.is_file():
            raise RuntimeError(f"Codex output schema bulunamadı: {schema_path}")
        args += ["--output-schema", str(schema_path)]
    image_paths = [str(Path(x).expanduser().resolve()) for x in (images or []) if Path(x).expanduser().is_file()]
    if resume_thread_id:
        args += ["resume", resume_thread_id]
        for path in image_paths:
            args += ["--image", path]
        args += [prompt]
    else:
        for path in image_paths:
            args += ["--image", path]
        args += [prompt]
    action = f"resume={resume_thread_id[:12]}" if resume_thread_id else "new-thread"
    log(task_id, "system", f"launch {action} model={model or 'default'} effort={reasoning_effort or 'default'} speed={service_tier or 'default'} mode={mode} cwd={workspace}")
    p = subprocess.Popen(
        args,
        cwd=workspace,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    with RUNNERS_LOCK:
        if task_id:
            RUNNERS[task_id] = p
    out, err = [], []

    def pump(stream, sink, name):
        for line in iter(stream.readline, ""):
            sink.append(line)
            if name == "stdout":
                record_codex_event(session_id, task_id or "", plan_id, line)
                display = pretty_codex_event(line)
            else:
                display = line.rstrip("\n")
            log(task_id, name, display)
        stream.close()

    t1 = threading.Thread(target=pump, args=(p.stdout, out, "stdout"), daemon=True)
    t2 = threading.Thread(target=pump, args=(p.stderr, err, "stderr"), daemon=True)
    t1.start(); t2.start()
    code = p.wait(); t1.join(); t2.join()
    with RUNNERS_LOCK:
        if task_id:
            RUNNERS.pop(task_id, None)
    stdout, stderr = "".join(out), "".join(err)
    final = parse_codex_final(stdout)
    if code != 0:
        current_status = (one("SELECT status FROM tasks WHERE id=?", (task_id,)) or {}).get("status") if task_id else ""
        stopped = current_status in ("cancelled", "paused_by_user", "pausing") or plan_is_paused(plan_id)
        finish_agent_session(session_id, "cancelled" if stopped else "failed", final)
        raise RuntimeError((stderr or stdout or f"process exit {code}")[-12000:])
    finish_agent_session(session_id, "completed", final)
    return final

def steer_app_server(task_id, message_id, prompt, image_paths=None):
    """Deliver a user message to an active App Server turn when possible."""
    with APP_SERVER_CONTROLS_LOCK:
        control = APP_SERVER_CONTROLS.get(task_id)
        if not control:
            return False
        request_id = control["next_request_id"]
        control["next_request_id"] += 1
        control["pending"][request_id] = message_id
        thread_id = control["thread_id"]
        turn_id = control["turn_id"]

    request = {
        "id": request_id,
        "method": "turn/steer",
        "params": {
            "threadId": thread_id,
            "input": app_server_input_items(prompt, image_paths, task_id),
            "expectedTurnId": turn_id,
        },
    }
    try:
        control["send"](request)
        log(task_id, "manual", "user message sent to the active App Server turn")
        return True
    except Exception as exc:
        with APP_SERVER_CONTROLS_LOCK:
            control["pending"].pop(request_id, None)
        execute("UPDATE task_messages SET status=?,error=? WHERE id=?", ("failed", str(exc), message_id))
        log(task_id, "manual", f"App Server steer could not be sent: {exc}")
        return False

def interrupt_app_server(task_id):
    """Ask an active App Server turn to stop without opening a new thread."""
    with APP_SERVER_CONTROLS_LOCK:
        control = APP_SERVER_CONTROLS.get(task_id)
        interrupt = (control or {}).get("interrupt")
    if not interrupt:
        return False
    try:
        interrupt()
        log(task_id, "supervisor", "interrupt requested; preserving this worker conversation for resume")
        return True
    except Exception as exc:
        log(task_id, "stderr", f"App Server interrupt could not be sent: {exc}")
        return False

def orchestrator_models(requested):
    if requested == "auto-best":
        return ["gpt-6-astra", "gpt-5.6-sol"]
    return [requested or DEFAULT_ORCHESTRATOR]

def run_orchestrator(prompt, workspace, requested_model, task_id=None, reasoning_effort="", service_tier="default", mode="read", transient_retries=0, images=None, output_schema="", resume_thread_id="", session_kind="orchestrator"):
    errors = []
    bound_thread_id = resume_thread_id or ""
    for model in orchestrator_models(requested_model):
        attempt = 0
        while True:
            try:
                return run_codex(
                    prompt, workspace, mode, model, task_id, reasoning_effort, service_tier,
                    images=images, resume_thread_id=resume_thread_id, session_kind=session_kind,
                    output_schema=output_schema,
                ), model
            except Exception as e:
                err = str(e)
                if task_id and plan_is_paused(infer_plan_id(task_id)):
                    # A user pause must stop model fallback/retry as well. The
                    # persisted mission state owns the later resume decision.
                    raise
                if attempt < transient_retries and is_transient_error(err):
                    attempt += 1
                    delay = 2 if attempt == 1 else 5
                    log(task_id, "supervisor", f"self-heal: orchestrator transient failure on {model}; retry {attempt}/{transient_retries} in {delay}s")
                    time.sleep(delay)
                    continue
                errors.append(f"{model}: {e}")
                if not bound_thread_id:
                    latest = latest_orchestrator_session(infer_plan_id(task_id)) if task_id else None
                    bound_thread_id = (latest or {}).get("thread_id") or ""
                    resume_thread_id = bound_thread_id
                break
        if requested_model != "auto-best":
            break
    raise RuntimeError("\n\n".join(errors))
