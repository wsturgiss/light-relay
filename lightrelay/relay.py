"""The relay: tailnet-only, published with Tailscale Serve, never Funnel.

The agent sends it messages for the phone (POST /notify) and collects replies
from it (GET /replies, POST /replies/ack). Every route but /healthz accepts
only the tailnet devices listed in ALLOWED_PEERS.

A message is signed and kept in the outbox, where the Relay Inbox tool fetches
it through the inbox's GET /messages. If the tool has a UnifiedPush endpoint on
Light's push server, the message is also pushed there. Either way the relay
never needs a route to the phone.
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from .common import (REPLY_COLUMNS, HttpError, Handler, RateLimiter, env, env_int,
                     log, parse_ips, reply_from_row, serve, setup_logging)

MAX_HEADLINE = 120
MAX_DETAIL = 1000
MAX_CHOICES = 4
MAX_CHOICE = 24
REF_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
# A message the relay sent (m_), or a conversation started on the phone (t_).
THREAD_ID_RE = re.compile(r"^[mt]_[0-9a-f]{16}$")
RETAIN_DAYS = 90
MAX_ROWS = 5000

OUTBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    seq     INTEGER PRIMARY KEY AUTOINCREMENT,
    id      TEXT NOT NULL UNIQUE,
    at      TEXT NOT NULL,
    payload TEXT NOT NULL,
    body    TEXT NOT NULL
);
"""


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_message(content_type, body):
    """A message is plain text (first line headline, the rest detail) or JSON."""
    try:
        raw = body.decode("utf-8")
    except UnicodeDecodeError:
        raise HttpError(400, "body must be UTF-8")

    if content_type.startswith("application/json"):
        try:
            data = json.loads(raw)
        except ValueError:
            raise HttpError(400, "body is not JSON")
        if not isinstance(data, dict):
            raise HttpError(400, "body must be a JSON object")
        headline = data.get("headline")
        detail = data.get("detail", "")
        choices = data.get("choices", [])
        ref = data.get("ref")
        thread = data.get("thread")
    else:
        headline, _, detail = raw.strip().partition("\n")
        choices, ref, thread = [], None, None

    if not isinstance(headline, str) or not headline.strip():
        raise HttpError(400, "headline is required")
    headline = headline.strip()
    if len(headline) > MAX_HEADLINE:
        raise HttpError(400, f"headline is over {MAX_HEADLINE} chars")
    if not isinstance(detail, str):
        raise HttpError(400, "detail must be a string")
    detail = detail.strip()
    if len(detail) > MAX_DETAIL:
        raise HttpError(400, f"detail is over {MAX_DETAIL} chars")
    if (not isinstance(choices, list) or len(choices) > MAX_CHOICES
            or not all(isinstance(c, str) and c.strip() and len(c.strip()) <= MAX_CHOICE for c in choices)):
        raise HttpError(400, f"choices must be up to {MAX_CHOICES} strings of at most {MAX_CHOICE} chars")
    if ref is not None and (not isinstance(ref, str) or not REF_RE.match(ref)):
        raise HttpError(400, "ref must be 1-64 of [A-Za-z0-9_.:-]")
    if thread is not None and (not isinstance(thread, str) or not THREAD_ID_RE.match(thread)):
        raise HttpError(400, "thread must be a message id (m_…) or a reply's thread (t_…)")

    message = {
        "v": 1,
        "id": "m_" + secrets.token_hex(8),
        "at": now_iso(),
        "headline": headline,
        "detail": detail,
        "choices": [c.strip() for c in choices],
    }
    if ref:
        message["ref"] = ref
    if thread:
        message["thread"] = thread
    return message


def sign(message, key):
    """The push body: `v1.<hex HMAC-SHA256 of the JSON>.<JSON>`.

    Anyone who learns the push endpoint can post to it; the tool drops anything
    that doesn't carry a valid signature under the key it generated at pairing.
    """
    return sign_payload(json.dumps(message, separators=(",", ":"), ensure_ascii=False), key)


def sign_payload(payload, key):
    mac = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()
    return f"v1.{mac}.{payload}".encode()


class Outbox:
    """Every signed message, for the tool to fetch through the inbox, which
    mounts this database read-only. Kept whether or not the push got through."""

    def __init__(self, path):
        # Rollback journal, not WAL: the inbox opens this file read-only from
        # another container, and a WAL reader needs write access to -shm.
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(OUTBOX_SCHEMA)
        self.lock = threading.Lock()

    def add(self, message, body):
        payload = body.decode().split(".", 2)[2]
        with self.lock:
            self.db.execute("INSERT INTO messages (id, at, payload, body) VALUES (?, ?, ?, ?)",
                            (message["id"], message["at"], payload, body.decode()))
            self._prune()

    def thread_of(self, message_id):
        """The conversation `message_id` belongs to: the id of its first message.
        An id the outbox doesn't know (pruned, or from before the outbox) starts
        its own conversation."""
        with self.lock:
            row = self.db.execute("SELECT payload FROM messages WHERE id = ?", (message_id,)).fetchone()
        return json.loads(row[0]).get("thread", message_id) if row else message_id

    def resign(self, key):
        """Sign everything kept under `key`. New keys on the phone (or a reinstall)
        would otherwise leave the whole history failing its signature check."""
        with self.lock:
            changed = []
            for seq, payload, body in self.db.execute("SELECT seq, payload, body FROM messages").fetchall():
                signed = sign_payload(payload, key).decode()
                if signed != body:
                    changed.append((signed, seq))
            if changed:
                self.db.executemany("UPDATE messages SET body = ? WHERE seq = ?", changed)
            return len(changed)

    def _prune(self):
        cutoff = datetime.fromtimestamp(time.time() - RETAIN_DAYS * 86400, timezone.utc)
        self.db.execute("DELETE FROM messages WHERE at < ?", (cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),))
        self.db.execute(
            "DELETE FROM messages WHERE seq <= (SELECT MAX(seq) FROM messages) - ?", (MAX_ROWS,))


class Cursor:
    """How far through the inbox the agent has acknowledged. Lives in the
    relay's own data dir: the inbox database is mounted read-only here."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()

    def get(self):
        try:
            with open(self.path) as f:
                return int(json.load(f)["seq"])
        except (FileNotFoundError, ValueError, KeyError):
            return 0

    def advance(self, seq):
        with self.lock:
            seq = max(seq, self.get())
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"seq": seq}, f)
            os.replace(tmp, self.path)
            return seq


def read_replies(db_path, after, limit):
    if not os.path.exists(db_path):
        return []
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        db.execute("PRAGMA busy_timeout=5000")
        rows = db.execute(f"SELECT {REPLY_COLUMNS} FROM replies WHERE seq > ? ORDER BY seq LIMIT ?",
                          (after, limit)).fetchall()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return []
        raise
    finally:
        db.close()
    return [reply_from_row(r) for r in rows]


class RelayHandler(Handler):
    allowed_peers = frozenset()
    push_endpoint = None
    push_key = None
    push_timeout = 15
    notify_limit = None
    outbox = None
    inbox_db = None
    cursor = None

    def route(self, method):
        url = urlsplit(self.path)
        if method == "GET" and url.path == "/healthz":
            return self.send_json(200, {"ok": True, "paired": bool(self.push_key), "push": bool(self.push_endpoint)})

        ip = self.client_ip()
        if ip not in self.allowed_peers:
            log.warning("refused %s %s from %s", method, url.path, ip)
            raise HttpError(403, "forbidden")

        if method == "POST" and url.path == "/notify":
            return self.notify()
        if method == "GET" and url.path == "/replies":
            return self.replies(parse_qs(url.query))
        if method == "POST" and url.path == "/replies/ack":
            return self.ack()
        raise HttpError(404, "not found")

    def notify(self):
        if not self.push_key:
            raise HttpError(503, "not paired: set PUSH_KEY")
        message = parse_message(self.headers.get("Content-Type", "text/plain"), self.read_body())
        if not self.notify_limit.allow("notify"):
            raise HttpError(429, "notify rate limit reached")
        if "thread" in message:
            # The agent may name any message in the conversation; the phone groups by the first.
            message["thread"] = self.outbox.thread_of(message["thread"])

        body = sign(message, self.push_key)
        self.outbox.add(message, body)
        pushed = self.push(message, body) if self.push_endpoint else False
        log.info("stored %s (%s)", message["id"], "pushed" if pushed else "waiting to be fetched")
        self.send_json(202, {"id": message["id"], "at": message["at"],
                             "thread": message.get("thread", message["id"]), "pushed": pushed})

    def push(self, message, body):
        """Best effort: a message that doesn't push still waits in the outbox,
        so a failure here is logged, not handed back to the agent to retry."""
        request = urllib.request.Request(
            self.push_endpoint,
            data=body,
            method="POST",
            headers={"Content-Type": "text/plain; charset=utf-8", "TTL": "86400", "Urgency": "high"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.push_timeout) as resp:
                status = resp.status
        except urllib.error.HTTPError as e:
            with e:
                status = e.code
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            log.warning("push for %s failed: %s", message["id"], e)
            return False
        if not 200 <= status < 300:
            log.warning("push for %s rejected with HTTP %d", message["id"], status)
            return False
        return True

    def replies(self, query):
        try:
            limit = min(max(int(query.get("limit", ["50"])[0]), 1), 200)
            after = int(query["after"][0]) if "after" in query else self.cursor.get()
        except ValueError:
            raise HttpError(400, "after and limit must be integers")
        rows = read_replies(self.inbox_db, after, limit)
        self.send_json(200, {"replies": rows, "cursor": self.cursor.get()})

    def ack(self):
        data = self.read_json()
        up_to = data.get("upTo")
        if not isinstance(up_to, int) or isinstance(up_to, bool) or up_to < 0:
            raise HttpError(400, "upTo must be a non-negative integer (a reply's seq)")
        self.send_json(200, {"cursor": self.cursor.advance(up_to)})


def main():
    setup_logging()
    data_dir = env("DATA_DIR", "/data")
    allowed = parse_ips(env("ALLOWED_PEERS", required=True))

    RelayHandler.allowed_peers = allowed
    RelayHandler.push_endpoint = env("PUSH_ENDPOINT")
    push_key = env("PUSH_KEY")
    RelayHandler.push_key = push_key.encode() if push_key else None
    RelayHandler.push_timeout = env_int("PUSH_TIMEOUT", 15)
    RelayHandler.notify_limit = RateLimiter(env_int("NOTIFY_RATE_PER_HOUR", 60), 3600)
    RelayHandler.outbox = Outbox(env("OUTBOX_DB", f"{data_dir}/outbox.db"))
    if RelayHandler.push_key and (n := RelayHandler.outbox.resign(RelayHandler.push_key)):
        log.info("re-signed %d kept message(s) under the current PUSH_KEY", n)
    RelayHandler.inbox_db = env("INBOX_DB", "/inbox/inbox.db")
    RelayHandler.cursor = Cursor(f"{data_dir}/cursor.json")
    RelayHandler.trusted_proxies = parse_ips(env("TRUSTED_PROXIES", "127.0.0.1,::1"))
    RelayHandler.client_ip_header = env("CLIENT_IP_HEADER", "X-Forwarded-For")
    RelayHandler.max_body = env_int("RELAY_MAX_BODY", 8192)

    if not RelayHandler.push_key:
        log.warning("not paired yet: /notify will answer 503 until PUSH_KEY is set")
    elif not RelayHandler.push_endpoint:
        log.info("no PUSH_ENDPOINT: messages wait in the outbox for the tool to fetch")
    log.info("allowed peers: %s", ", ".join(sorted(map(str, allowed))))

    serve(RelayHandler, env("HOST", "0.0.0.0"), env_int("PORT", 8080)).serve_forever()


if __name__ == "__main__":
    main()
