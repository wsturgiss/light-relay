"""The reply inbox: the one public door, published with Tailscale Funnel.

It accepts replies from the Relay Inbox tool and does nothing else. There is no
way to list, read or delete anything through it, and it has no route to the
phone-pushing path. The relay reads the database it writes, through a
read-only mount in its own container.
"""

import hmac
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone

from .common import (HttpError, Handler, RateLimiter, env, env_int, log,
                     parse_ips, serve, setup_logging)

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_TEXT = 1000
MAX_CHOICE = 64
RETAIN_DAYS = 30
MAX_ROWS = 1000

SCHEMA = """
CREATE TABLE IF NOT EXISTS replies (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    id          TEXT NOT NULL UNIQUE,
    message_id  TEXT,
    choice      TEXT,
    text        TEXT,
    sent_at     TEXT,
    received_at TEXT NOT NULL
);
"""


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ReplyStore:
    def __init__(self, path):
        # Rollback journal, not WAL: the relay opens this file read-only from
        # another container, and a WAL reader needs write access to -shm.
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()

    def add(self, reply):
        """Store a reply. Returns False if its id was already stored (a retry)."""
        with self.lock:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO replies (id, message_id, choice, text, sent_at, received_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (reply["id"], reply.get("messageId"), reply.get("choice"),
                 reply.get("text"), reply.get("sentAt"), now_iso()),
            )
            self._prune()
            return cur.rowcount == 1

    def _prune(self):
        cutoff = datetime.fromtimestamp(time.time() - RETAIN_DAYS * 86400, timezone.utc)
        self.db.execute("DELETE FROM replies WHERE received_at < ?",
                        (cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),))
        self.db.execute(
            "DELETE FROM replies WHERE seq <= (SELECT MAX(seq) FROM replies) - ?", (MAX_ROWS,))


def validate(data):
    reply_id = data.get("id")
    if not isinstance(reply_id, str) or not ID_RE.match(reply_id):
        raise HttpError(400, "id must be 1-64 of [A-Za-z0-9_-]")
    message_id = data.get("messageId")
    if message_id is not None and (not isinstance(message_id, str) or not ID_RE.match(message_id)):
        raise HttpError(400, "bad messageId")
    choice = data.get("choice")
    if choice is not None and (not isinstance(choice, str) or len(choice) > MAX_CHOICE):
        raise HttpError(400, f"choice must be a string of at most {MAX_CHOICE} chars")
    text = data.get("text")
    if text is not None and (not isinstance(text, str) or len(text) > MAX_TEXT):
        raise HttpError(400, f"text must be a string of at most {MAX_TEXT} chars")
    if not (choice or (text and text.strip())):
        raise HttpError(400, "a reply needs a choice or text")
    sent_at = data.get("sentAt")
    if sent_at is not None and (not isinstance(sent_at, str) or len(sent_at) > 40):
        raise HttpError(400, "bad sentAt")
    return {"id": reply_id, "messageId": message_id, "choice": choice or None,
            "text": text or None, "sentAt": sent_at}


class InboxHandler(Handler):
    store = None
    token = b""
    per_ip = None
    overall = None

    def route(self, method):
        ip = self.client_ip()
        # Everything counts against the sender, junk included.
        if not self.per_ip.allow(ip) or not self.overall.allow("all"):
            raise HttpError(429, "slow down")
        if method != "POST" or self.path != "/replies":
            raise HttpError(404, "not found")
        # Reject before reading a byte of the body.
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or not hmac.compare_digest(auth[7:].strip().encode(), self.token):
            raise HttpError(401, "unauthorized")
        reply = validate(self.read_json())
        stored = self.store.add(reply)
        log.info("reply %s from %s (%s)", reply["id"], ip, "stored" if stored else "duplicate")
        self.send_json(202, {"ok": True})


def main():
    setup_logging()
    token = env("REPLY_TOKEN", required=True)
    if len(token) < 32:
        raise SystemExit("REPLY_TOKEN is too short; use the one the phone tool generated")
    data_dir = env("DATA_DIR", "/data")

    InboxHandler.store = ReplyStore(f"{data_dir}/inbox.db")
    InboxHandler.token = token.encode()
    InboxHandler.per_ip = RateLimiter(env_int("INBOX_RATE_PER_MIN", 20), 60)
    InboxHandler.overall = RateLimiter(env_int("INBOX_RATE_TOTAL_PER_MIN", 120), 60)
    InboxHandler.trusted_proxies = parse_ips(env("TRUSTED_PROXIES", "127.0.0.1,::1"))
    InboxHandler.client_ip_header = env("CLIENT_IP_HEADER", "X-Forwarded-For")
    InboxHandler.max_body = env_int("INBOX_MAX_BODY", 4096)

    serve(InboxHandler, env("HOST", "0.0.0.0"), env_int("PORT", 8081)).serve_forever()


if __name__ == "__main__":
    main()
