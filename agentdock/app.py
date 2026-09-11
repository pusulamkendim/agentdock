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

from . import config
from .api import Handler
from .db import init_db as init_storage_db
from .version import VERSION


def initialize_database():
    """Initialize persistence, schemas, and restart recovery in order."""
    init_storage_db()
    from .git_ops import repo_info
    from .mission import (
        migrate_legacy_orchestrator_state,
        recover_orphaned_runs,
    )
    from .db import materialize_existing_workspaces
    from .schemas import consultation_schema_path, planner_schema_path
    from .timeline import write_mission_docs

    planner_schema_path()
    consultation_schema_path()
    recover_orphaned_runs(write_docs=write_mission_docs)
    materialize_existing_workspaces(repo_info_func=repo_info)
    migrate_legacy_orchestrator_state(write_docs=write_mission_docs)


def main():
    initialize_database()
    url = f"http://{config.HOST}:{config.PORT}"
    print(f"AgentDock v{VERSION} running at {url}")
    print("Auth mode: Codex CLI signed in with ChatGPT (Plus supported).")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    ThreadingHTTPServer((config.HOST, config.PORT), Handler).serve_forever()
