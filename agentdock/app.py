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

def main():
    init_db()
    url = f"http://{HOST}:{PORT}"
    print(f"AgentDock v0.12 running at {url}")
    print("Auth mode: Codex CLI signed in with ChatGPT (Plus supported).")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
