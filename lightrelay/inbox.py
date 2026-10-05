"""The reply inbox: the one public door, published with Tailscale Funnel.

Everything on it is for the Relay Inbox tool and behind the reply token: it
accepts replies (POST /replies), and it hands back the conversation so far
(GET /messages: the relay's signed messages, GET /replies: the replies stored
here), so the tool can fetch messages without a push endpoint and rebuild its
history. Nothing can be deleted through it, and it has no route to the
phone-pushing path. It reads the relay's outbox through a read-only mount,
and the relay reads the reply database it writes the same way.
"""

import hmac
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from .common import (REPLY_COLUMNS, HttpError, Handler, RateLimiter, env, env_int,
                     log, parse_ips, reply_from_row, serve, setup_logging)

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_TEXT = 1000
MAX_CHOICE = 64
RETAIN_DAYS = 90
MAX_ROWS = 5000
MAX_FETCH = 100
MAX_TITLE = 120
# A conversation started on the phone. The agent continues it with notify's `thread`.
THREAD_RE = re.compile(r"^t_[0-9a-f]{16}$")

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
        # Added with conversations started on the phone; older databases lack them.
        have = {row[1] for row in self.db.execute("PRAGMA table_info(replies)")}
        for column in ("thread", "title"):
            if column not in have:
                self.db.execute(f"ALTER TABLE replies ADD COLUMN {column} TEXT")
        self.lock = threading.Lock()

    def add(self, reply):
        """Store a reply. Returns False if its id was already stored (a retry)."""
        with self.lock:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO replies"
                " (id, message_id, thread, title, choice, text, sent_at, received_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (reply["id"], reply.get("messageId"), reply.get("thread"), reply.get("title"),
                 reply.get("choice"), reply.get("text"), reply.get("sentAt"), now_iso()),
            )
            self._prune()
            return cur.rowcount == 1

    def since(self, after, limit):
        with self.lock:
            rows = self.db.execute(
                f"SELECT {REPLY_COLUMNS} FROM replies WHERE seq > ? ORDER BY seq LIMIT ?",
                (after, limit)).fetchall()
        return [reply_from_row(r) for r in rows]

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
    thread = data.get("thread")
    if thread is not None and (not isinstance(thread, str) or not THREAD_RE.match(thread)):
        raise HttpError(400, "thread must be t_ and 16 hex digits")
    title = data.get("title")
    if title is not None and (not isinstance(title, str) or len(title) > MAX_TITLE):
        raise HttpError(400, f"title must be a string of at most {MAX_TITLE} chars")
    if title and not thread:
        raise HttpError(400, "a title starts a conversation, so it needs a thread")
    return {"id": reply_id, "messageId": message_id, "thread": thread, "title": title or None,
            "choice": choice or None, "text": text or None, "sentAt": sent_at}


def read_messages(db_path, after, limit):
    """Signed messages from the relay's outbox, oldest first."""
    if not os.path.exists(db_path):
        return []
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        db.execute("PRAGMA busy_timeout=5000")
        rows = db.execute("SELECT seq, body FROM messages WHERE seq > ? ORDER BY seq LIMIT ?",
                          (after, limit)).fetchall()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return []
        raise
    finally:
        db.close()
    return [{"seq": r[0], "body": r[1]} for r in rows]


def paging(query):
    """`after` is the last seq the tool already has; the tool keeps its own place."""
    try:
        after = max(int(query.get("after", ["0"])[0]), 0)
        limit = min(max(int(query.get("limit", ["50"])[0]), 1), MAX_FETCH)
    except ValueError:
        raise HttpError(400, "after and limit must be integers")
    return after, limit


class InboxHandler(Handler):
    store = None
    outbox_db = None
    token = b""
    per_ip = None
    overall = None

    def route(self, method):
        ip = self.client_ip()
        # Everything counts against the sender, junk included.
        if not self.per_ip.allow(ip) or not self.overall.allow("all"):
            raise HttpError(429, "slow down")
        url = urlsplit(self.path)
        if (method, url.path) == ("POST", "/replies"):
            handler = self.post_reply
        elif (method, url.path) == ("GET", "/messages"):
            handler = self.get_messages
        elif (method, url.path) == ("GET", "/replies"):
            handler = self.get_replies
        else:
            raise HttpError(404, "not found")
        # Reject before reading a byte of the body.
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or not hmac.compare_digest(auth[7:].strip().encode(), self.token):
            raise HttpError(401, "unauthorized")
        handler(ip, parse_qs(url.query))

    def post_reply(self, ip, _query):
        reply = validate(self.read_json())
        stored = self.store.add(reply)
        log.info("reply %s from %s (%s)", reply["id"], ip, "stored" if stored else "duplicate")
        self.send_json(202, {"ok": True})

    def get_messages(self, ip, query):
        after, limit = paging(query)
        messages = read_messages(self.outbox_db, after, limit)
        log.info("%d message(s) after %d to %s", len(messages), after, ip)
        self.send_json(200, {"messages": messages})

    def get_replies(self, ip, query):
        after, limit = paging(query)
        replies = self.store.since(after, limit)
        log.info("%d reply(s) after %d to %s", len(replies), after, ip)
        self.send_json(200, {"replies": replies})


def main():
    setup_logging()
    token = env("REPLY_TOKEN", required=True)
    if len(token) < 32:
        raise SystemExit("REPLY_TOKEN is too short; use the one the phone tool generated")
    data_dir = env("DATA_DIR", "/data")

    InboxHandler.store = ReplyStore(f"{data_dir}/inbox.db")
    InboxHandler.outbox_db = env("OUTBOX_DB", "/outbox/outbox.db")
    InboxHandler.token = token.encode()
    InboxHandler.per_ip = RateLimiter(env_int("INBOX_RATE_PER_MIN", 20), 60)
    InboxHandler.overall = RateLimiter(env_int("INBOX_RATE_TOTAL_PER_MIN", 120), 60)
    InboxHandler.trusted_proxies = parse_ips(env("TRUSTED_PROXIES", "127.0.0.1,::1"))
    InboxHandler.client_ip_header = env("CLIENT_IP_HEADER", "X-Forwarded-For")
    InboxHandler.max_body = env_int("INBOX_MAX_BODY", 4096)

    serve(InboxHandler, env("HOST", "0.0.0.0"), env_int("PORT", 8081)).serve_forever()


if __name__ == "__main__":
    main()
