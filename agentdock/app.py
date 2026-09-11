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
from .db import init_db

def main():
    init_db()
    url = f"http://{config.HOST}:{config.PORT}"
    print(f"AgentDock v0.13.0 running at {url}")
    print("Auth mode: Codex CLI signed in with ChatGPT (Plus supported).")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    ThreadingHTTPServer((config.HOST, config.PORT), Handler).serve_forever()
