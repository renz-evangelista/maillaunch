"""The append-only send log.

One file per day under ~/.maillaunch/logs/, which is what makes `maillaunch log`
a file read rather than a query, and makes "what did I send on the 13th?"
answerable with cat.

Line format follows the brief's sample, with one change: the brief's
requirements say ISO 8601 while its sample shows `2025-05-13 14:22:01`, which is
not. ISO 8601 wins, including the UTC offset -- a send log without a timezone is
ambiguous the moment anyone travels or a server moves.
"""

from datetime import datetime
from pathlib import Path

from .config import HOME

SENT = "SENT"
FAILED = "FAILED"
RETRY = "RETRY"
SKIPPED = "SKIPPED"
LIMIT_REACHED = "LIMIT_REACHED"


def log_path(day=None, root=None):
    directory = Path(root) if root else HOME / "logs"
    stamp = (day or datetime.now().astimezone()).strftime("%Y-%m-%d")
    return directory / f"{stamp}.log"


def format_line(status, campaign_id, email=None, name=None, detail=None, now=None):
    """Render one log line.

        [2026-10-09T14:22:01+08:00] SENT     john@acme.com (John Smith) | campaign: c_...
    """
    timestamp = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
    who = email or ""
    if email and name:
        who = f"{email} ({name})"
    parts = [f"[{timestamp}]", f"{status:<13}", f"{who:<34}".rstrip() if who else ""]
    line = " ".join(part for part in parts if part).rstrip()
    line += f" | campaign: {campaign_id}"
    if detail:
        line += f" | {detail}"
    return line


def write(status, campaign_id, email=None, name=None, detail=None, root=None, echo=True):
    """Append a line to today's log, and by default mirror it to stdout.

    The echo matters: a send takes hours, and a user watching the terminal should
    see the same thing the file records rather than silence.
    """
    line = format_line(status, campaign_id, email, name, detail)
    path = log_path(root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    if echo:
        print(line, flush=True)
    return line


def read(day=None, root=None):
    path = log_path(day=day, root=root)
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")
