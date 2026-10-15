"""Campaign state, in SQLite.

SQLite rather than a JSON file for one reason: a send runs for hours and can be
killed at any moment. Every send is committed in its own transaction, so the
worst a Ctrl+C or a crash can cost is the one email in flight -- and `resume`
reads the same rows back to find out exactly where it stopped.

sqlite3 is in the standard library, so this costs no dependency.
"""

import sqlite3
from datetime import datetime
from pathlib import Path

from .config import HOME

PENDING, SENT, FAILED, SKIPPED = "PENDING", "SENT", "FAILED", "SKIPPED"
RUNNING, PAUSED, DONE = "running", "paused", "done"

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id           TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    provider     TEXT NOT NULL,
    subject      TEXT NOT NULL,
    body         TEXT NOT NULL,
    csv_path     TEXT NOT NULL,
    status       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recipients (
    campaign_id  TEXT NOT NULL,
    row_index    INTEGER NOT NULL,
    email        TEXT NOT NULL,
    name         TEXT,
    data         TEXT NOT NULL,
    status       TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    sent_at      TEXT,
    PRIMARY KEY (campaign_id, row_index)
);

-- The daily limit counts SENT rows for today, so give that count an index to
-- seek rather than a table to scan.
CREATE INDEX IF NOT EXISTS recipients_sent_at_idx ON recipients (status, sent_at);
"""


def connect(path=None):
    """Open (and if needed create) the state database."""
    db_path = Path(path) if path else HOME / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    return connection


def new_campaign_id(connection, now=None):
    """`c_YYYYMMDD_NNN`, the format from the brief's log sample."""
    today = (now or datetime.now()).strftime("%Y%m%d")
    prefix = f"c_{today}_"
    row = connection.execute(
        "SELECT id FROM campaigns WHERE id LIKE ? ORDER BY id DESC LIMIT 1",
        (prefix + "%",),
    ).fetchone()
    nth = int(row["id"].rsplit("_", 1)[1]) + 1 if row else 1
    return f"{prefix}{nth:03d}"


def create_campaign(connection, campaign_id, provider, subject, body, csv_path, rows):
    """Record a campaign and its full recipient list up front.

    Writing every recipient now -- including the ones already known to be
    unsendable -- is what makes `resume` simple: the queue is a table, and
    position in it is just "the PENDING rows, in order".
    """
    import json

    with connection:
        connection.execute(
            "INSERT INTO campaigns (id, created_at, provider, subject, body, csv_path, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                campaign_id,
                datetime.now().astimezone().isoformat(timespec="seconds"),
                provider,
                subject,
                body,
                str(csv_path),
                RUNNING,
            ),
        )
        connection.executemany(
            "INSERT INTO recipients (campaign_id, row_index, email, name, data, status, error)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    campaign_id,
                    row["row"],
                    row["email"],
                    row["name"],
                    json.dumps(row["data"]),
                    SKIPPED if row["skip_reason"] else PENDING,
                    row["skip_reason"],
                )
                for row in rows
            ],
        )


def get_campaign(connection, campaign_id):
    return connection.execute(
        "SELECT * FROM campaigns WHERE id = ?", (campaign_id,)
    ).fetchone()


def latest_resumable(connection):
    """The most recent campaign that still has work left."""
    return connection.execute(
        "SELECT * FROM campaigns WHERE status IN (?, ?) ORDER BY created_at DESC, id DESC LIMIT 1",
        (RUNNING, PAUSED),
    ).fetchone()


def pending(connection, campaign_id):
    """Recipients still to send, oldest row first."""
    import json

    rows = connection.execute(
        "SELECT * FROM recipients WHERE campaign_id = ? AND status = ? ORDER BY row_index",
        (campaign_id, PENDING),
    ).fetchall()
    return [
        {
            "row": r["row_index"],
            "email": r["email"],
            "name": r["name"],
            "data": json.loads(r["data"]),
            "attempts": r["attempts"],
        }
        for r in rows
    ]


def mark(connection, campaign_id, row_index, status, error=None, attempts=None):
    """Record the outcome for one recipient. Its own transaction, on purpose."""
    sent_at = (
        datetime.now().astimezone().isoformat(timespec="seconds") if status == SENT else None
    )
    with connection:
        if attempts is None:
            connection.execute(
                "UPDATE recipients SET status = ?, error = ?, sent_at = ?"
                " WHERE campaign_id = ? AND row_index = ?",
                (status, error, sent_at, campaign_id, row_index),
            )
        else:
            connection.execute(
                "UPDATE recipients SET status = ?, error = ?, sent_at = ?, attempts = ?"
                " WHERE campaign_id = ? AND row_index = ?",
                (status, error, sent_at, attempts, campaign_id, row_index),
            )


def set_campaign_status(connection, campaign_id, status):
    with connection:
        connection.execute(
            "UPDATE campaigns SET status = ? WHERE id = ?", (status, campaign_id)
        )


def sent_today(connection, now=None):
    """How many emails actually went out today.

    Counts SENT rows only, across every campaign and both providers -- the limit
    protects the human's sending reputation, which is not per-campaign. Retries
    are not counted: a recipient that failed twice and succeeded once is one
    email, and one row.

    "Today" is the local calendar day, matching what `status` prints and what a
    user means by "100 a day".
    """
    today = (now or datetime.now()).astimezone().strftime("%Y-%m-%d")
    row = connection.execute(
        "SELECT COUNT(*) AS n FROM recipients WHERE status = ? AND substr(sent_at, 1, 10) = ?",
        (SENT, today),
    ).fetchone()
    return row["n"]


def counts(connection, campaign_id):
    rows = connection.execute(
        "SELECT status, COUNT(*) AS n FROM recipients WHERE campaign_id = ? GROUP BY status",
        (campaign_id,),
    ).fetchall()
    return {r["status"]: r["n"] for r in rows}
