"""Small operating-system integrations used by the HTTP layer."""

import shlex
import subprocess
import sys
from pathlib import Path


def open_terminal_at(path):
    path = str(Path(path).expanduser().resolve())
    if not Path(path).is_dir():
        raise ValueError("Terminal klasörü artık mevcut değil")
    if sys.platform == "darwin":
        script = f'tell application "Terminal" to do script "cd {shlex.quote(path)}"'
        subprocess.Popen(
            ["osascript", "-e", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    raise ValueError("Open Terminal currently supports macOS only")


__all__ = ["open_terminal_at"]
