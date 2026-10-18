"""The reply inbox: the one public door, published with Tailscale Funnel.

Everything on it is for the Relay Inbox tool and behind the reply token: it
accepts replies (POST /replies) and the voice notes and photos they carry
(PUT /attachments/<id>), and it hands back the conversation so far
(GET /messages: the relay's signed messages, GET /images/<id>: a picture one of
them carries, GET /replies: the replies stored here), so the tool can fetch messages without a push endpoint and rebuild its
history. With sessions on (SESSION_HOST), it takes only short-lived session
tokens, which the phone renews on the LAN; see session.py. Nothing can be deleted through it, and it has no route to the
phone-pushing path. It reads the relay's outbox through a read-only mount,
and the relay reads the reply database it writes the same way.
"""

import glob
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import ssl
import threading
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from . import agents, images, session, transcribe, webhook
from .agents import AGENT_NAME_RE, MAX_AGENTS, parse_agents  # noqa: F401 (inbox.parse_agents)
from .common import (ATTACHMENT_ID_RE, REPLY_COLUMNS, HttpError, Handler, RateLimiter, attachment_ids, env,
                     env_int, log, parse_ips, reply_from_row, serve, setup_logging, with_transcripts)

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_TEXT = 1000
# A choice, or the labels of several picked together (select "many"), joined with ", ".
MAX_CHOICE = 120
MAX_CHOICE_IDS = 4
RETAIN_DAYS = 90
MAX_ROWS = 5000
MAX_FETCH = 100
MAX_TITLE = 120
# A conversation started on the phone. The agent continues it with notify's `thread`.
THREAD_RE = re.compile(r"^t_[0-9a-f]{16}$")
MAX_ATTACHMENTS = 4


def _is_mp4(head):
    return head[4:8] == b"ftyp"


# What the phone may send, and how each type starts. The declared type has to
# match the bytes, so the agent never opens a file as something it isn't.
ATTACHMENT_TYPES = {
    "audio/ogg": lambda h: h.startswith(b"OggS"),
    "audio/webm": lambda h: h.startswith(b"\x1a\x45\xdf\xa3"),
    "audio/mp4": _is_mp4,
    "image/jpeg": lambda h: h.startswith(b"\xff\xd8\xff"),
    "image/png": lambda h: h.startswith(b"\x89PNG\r\n\x1a\n"),
    "image/webp": lambda h: h[:4] == b"RIFF" and h[8:12] == b"WEBP",
}

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
CREATE TABLE IF NOT EXISTS attachments (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    bytes       INTEGER NOT NULL,
    sha256      TEXT NOT NULL,
    received_at TEXT NOT NULL
);
-- A voice note's text, from the transcription service. Its own feed (seq), since
-- it usually lands after the phone has already fetched the reply. It outlives the
-- audio, as the reply does.
CREATE TABLE IF NOT EXISTS transcripts (
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    attachment_id TEXT NOT NULL UNIQUE,
    text          TEXT NOT NULL,
    at            TEXT NOT NULL,
    error         TEXT
);
"""


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def days_ago(days):
    return datetime.fromtimestamp(time.time() - days * 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ReplyStore:
    def __init__(self, path, attachments_dir=None, attachment_days=30, attachment_total=500 << 20):
        # Rollback journal, not WAL: the relay opens this file read-only from
        # another container, and a WAL reader needs write access to -shm.
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SCHEMA)
        # Added with conversations started on the phone; older databases lack them.
        have = {row[1] for row in self.db.execute("PRAGMA table_info(replies)")}
        # attachments: added with voice notes and photos.
        # to_agent: added with choosing who a conversation is for.
        # choice_ids, other, answered_at, second_answer: added with question upgrades.
        for column, kind in (("thread", "TEXT"), ("title", "TEXT"), ("attachments", "TEXT"), ("to_agent", "TEXT"),
                             ("choice_ids", "TEXT"), ("other", "INTEGER"), ("answered_at", "TEXT"),
                             ("second_answer", "INTEGER")):
            if column not in have:
                self.db.execute(f"ALTER TABLE replies ADD COLUMN {column} {kind}")
        if "error" not in {row[1] for row in self.db.execute("PRAGMA table_info(transcripts)")}:
            self.db.execute("ALTER TABLE transcripts ADD COLUMN error TEXT")
        self.lock = threading.Lock()
        # Files sit next to the database, so the relay's read-only mount sees them too.
        self.attachments_dir = attachments_dir or os.path.join(os.path.dirname(os.path.abspath(path)), "attachments")
        os.makedirs(self.attachments_dir, exist_ok=True)
        for leftover in glob.glob(os.path.join(self.attachments_dir, "*.part")):
            os.remove(leftover)
        self.attachment_days = attachment_days
        self.attachment_total = attachment_total

    def attachment(self, attachment_id):
        """`{"id", "type", "bytes"}` for a stored attachment, or None."""
        with self.lock:
            row = self.db.execute("SELECT id, type, bytes FROM attachments WHERE id = ?",
                                  (attachment_id,)).fetchone()
        return {"id": row[0], "type": row[1], "bytes": row[2]} if row else None

    def add_attachment(self, attachment_id, content_type, write):
        """Store a file. `write(f)` copies it into `f` and returns its length.
        Returns False for a retry of a file already stored; raises 409 if the
        id was already used for different bytes."""
        final = os.path.join(self.attachments_dir, attachment_id)
        part = f"{final}.{secrets.token_hex(4)}.part"
        try:
            with open(part, "wb") as f:
                length = write(f)
            with open(part, "rb") as f:
                head = f.read(16)
            digest = _sha256(part)
            if length == 0 or not ATTACHMENT_TYPES[content_type](head):
                raise HttpError(400, f"body is not {content_type}")
            with self.lock:
                row = self.db.execute("SELECT sha256 FROM attachments WHERE id = ?",
                                      (attachment_id,)).fetchone()
                if row:
                    if row[0] != digest:
                        raise HttpError(409, "that attachment id holds a different file")
                    return False
                os.replace(part, final)
                self.db.execute(
                    "INSERT INTO attachments (id, type, bytes, sha256, received_at) VALUES (?, ?, ?, ?, ?)",
                    (attachment_id, content_type, length, digest, now_iso()))
                self._prune_attachments()
                return True
        finally:
            if os.path.exists(part):
                os.remove(part)

    def _prune_attachments(self):
        """Older than `attachment_days`, then oldest first until under `attachment_total`."""
        rows = self.db.execute("SELECT id, bytes, received_at FROM attachments"
                               " ORDER BY received_at DESC, rowid DESC").fetchall()
        cutoff, kept, gone = days_ago(self.attachment_days), 0, []
        for attachment_id, size, received_at in rows:
            kept += size
            if received_at < cutoff or kept > self.attachment_total:
                gone.append(attachment_id)
        for attachment_id in gone:
            self.db.execute("DELETE FROM attachments WHERE id = ?", (attachment_id,))
            try:
                os.remove(os.path.join(self.attachments_dir, attachment_id))
            except FileNotFoundError:
                pass

    def add_transcript(self, attachment_id, text, error=None):
        """The text, or why there is none. Replacing a row gives it a new seq, so a
        transcript that follows an error reaches the phone too."""
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO transcripts (attachment_id, text, at, error) VALUES (?, ?, ?, ?)",
                            (attachment_id, text, now_iso(), error))
            self.db.execute("DELETE FROM transcripts WHERE at < ?", (days_ago(RETAIN_DAYS),))

    def transcripts_since(self, after, limit):
        with self.lock:
            rows = self.db.execute("SELECT seq, attachment_id, text, error FROM transcripts WHERE seq > ?"
                                   " ORDER BY seq LIMIT ?", (after, limit)).fetchall()
        return [{"seq": r[0], "id": r[1], "text": r[2], **({"error": r[3]} if r[3] else {})} for r in rows]

    def transcripts_for(self, ids):
        if not ids:
            return {}
        with self.lock:
            rows = self.db.execute("SELECT attachment_id, text, error FROM transcripts WHERE attachment_id IN"
                                   f" ({','.join('?' * len(ids))})", ids).fetchall()
        return {r[0]: (r[1], r[2]) for r in rows}

    def untranscribed(self, days=1):
        """Recent voice notes with no transcript yet, or only an error, oldest first."""
        with self.lock:
            rows = self.db.execute(
                "SELECT id FROM attachments WHERE type LIKE 'audio/%' AND received_at >= ?"
                " AND id NOT IN (SELECT attachment_id FROM transcripts WHERE error IS NULL) ORDER BY received_at",
                (days_ago(days),)).fetchall()
        return [r[0] for r in rows]

    def add(self, reply):
        """Store a reply. Returns False if its id was already stored (a retry).

        A reply that answers a question (a choice, or the free-text "Other") records
        when the question was first answered. One that comes after that is kept, so
        nothing you sent is lost, but flagged `second_answer`."""
        now = now_iso()
        answers = bool(reply.get("messageId") and (reply.get("choice") or reply.get("choiceIds") or reply.get("other")))
        with self.lock:
            answered_at, second = None, False
            if answers:
                row = self.db.execute(
                    "SELECT MIN(received_at) FROM replies WHERE message_id = ? AND answered_at IS NOT NULL AND id != ?",
                    (reply["messageId"], reply["id"])).fetchone()
                answered_at, second = (row[0], True) if row and row[0] else (now, False)
            cur = self.db.execute(
                "INSERT OR IGNORE INTO replies"
                " (id, message_id, thread, title, choice, text, sent_at, received_at, attachments, to_agent,"
                " choice_ids, other, answered_at, second_answer)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (reply["id"], reply.get("messageId"), reply.get("thread"), reply.get("title"),
                 reply.get("choice"), reply.get("text"), reply.get("sentAt"), now,
                 json.dumps(reply["attachments"]) if reply.get("attachments") else None, reply.get("to"),
                 json.dumps(reply["choiceIds"]) if reply.get("choiceIds") else None,
                 1 if reply.get("other") else None, answered_at, 1 if second else None),
            )
            self._prune()
            return cur.rowcount == 1

    def answer_of(self, reply_id):
        """(answeredAt, secondAnswer) as stored for a reply."""
        with self.lock:
            row = self.db.execute("SELECT answered_at, second_answer FROM replies WHERE id = ?", (reply_id,)).fetchone()
        return (row[0], bool(row[1])) if row else (None, False)

    def seq_of(self, reply_id):
        with self.lock:
            row = self.db.execute("SELECT seq FROM replies WHERE id = ?", (reply_id,)).fetchone()
        return row[0] if row else 0

    def since(self, after, limit):
        with self.lock:
            rows = self.db.execute(
                f"SELECT {REPLY_COLUMNS} FROM replies WHERE seq > ? ORDER BY seq LIMIT ?",
                (after, limit)).fetchall()
        replies = [reply_from_row(r) for r in rows]
        return with_transcripts(replies, self.transcripts_for(attachment_ids(replies)))

    def _prune(self):
        self.db.execute("DELETE FROM replies WHERE received_at < ?", (days_ago(RETAIN_DAYS),))
        self.db.execute(
            "DELETE FROM replies WHERE seq <= (SELECT MAX(seq) FROM replies) - ?", (MAX_ROWS,))


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def check_answer(data, message):
    """choiceIds and other, checked against the question they answer when the outbox
    still has it. Returns (choiceIds or None, other, choice label(s) or None)."""
    from .common import message_options
    ids, other, choice = data.get("choiceIds"), data.get("other"), data.get("choice")
    other = False if other is None else other
    if ids is not None and (not isinstance(ids, list) or not 1 <= len(ids) <= MAX_CHOICE_IDS
                            or len(set(ids)) != len(ids) or not all(isinstance(i, str) and ID_RE.match(i) for i in ids)):
        raise HttpError(400, f"choiceIds must be 1-{MAX_CHOICE_IDS} distinct choice ids")
    if not isinstance(other, bool):
        raise HttpError(400, "other must be true or false")
    if (ids or other) and not data.get("messageId"):
        raise HttpError(400, "choiceIds and other answer a message, so they need a messageId")
    if ids and other:
        raise HttpError(400, "an answer is choiceIds or other, not both")
    if message is None:
        return ids or None, other, choice
    options = {o["id"]: o["label"] for o in message_options(message)}
    if ids:
        if not set(ids) <= set(options):
            raise HttpError(400, "choiceIds must be choices the message offers")
        if len(ids) > 1 and message.get("select") != "many":
            raise HttpError(400, "this message takes one choice")
        # The labels, in the message's order, are what the agent reads as `choice`.
        choice = ", ".join(label for cid, label in options.items() if cid in ids)
    if other and not message.get("other"):
        raise HttpError(400, "this message doesn't offer Other")
    return ids or None, other, choice


def outbox_message(db_path, message_id):
    """The message as the relay signed it, or None (not there, or no outbox)."""
    if not db_path or not message_id or not os.path.exists(db_path):
        return None
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        db.execute("PRAGMA busy_timeout=5000")
        row = db.execute("SELECT body FROM messages WHERE id = ?", (message_id,)).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        db.close()
    try:
        return json.loads(row[0].split(".", 2)[2]) if row else None
    except (ValueError, IndexError):
        return None


def validate(data, store, agents=(), outbox_db=None):
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
    ids = data.get("attachments") or []
    if (not isinstance(ids, list) or len(ids) > MAX_ATTACHMENTS or len(set(ids)) != len(ids)
            or not all(isinstance(a, str) and ATTACHMENT_ID_RE.match(a) for a in ids)):
        raise HttpError(400, f"attachments must be up to {MAX_ATTACHMENTS} distinct ids (a_ and 16 hex digits)")
    attachments = []
    for attachment_id in ids:
        found = store.attachment(attachment_id)
        if not found:
            # Upload first, then reply. The phone uploads again on this answer.
            raise HttpError(400, f"attachment {attachment_id} hasn't been uploaded")
        attachments.append(found)
    choice_ids, other, choice = check_answer(data, outbox_message(outbox_db, message_id) if (
        data.get("choiceIds") or data.get("other")) else None)
    if other and not (text and text.strip()):
        raise HttpError(400, "an Other answer needs text")
    if choice is not None and len(choice) > MAX_CHOICE:
        raise HttpError(400, f"choice must be a string of at most {MAX_CHOICE} chars")
    if not (choice or (text and text.strip()) or attachments):
        raise HttpError(400, "a reply needs a choice, text or an attachment")
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
    to = data.get("to")
    if to is not None:
        if not isinstance(to, str) or not thread or message_id:
            raise HttpError(400, "to names who a conversation started on the phone is for, so it needs a thread")
        # One of AGENTS, spelled as configured. A name since removed is kept as sent.
        to = next((a for a in agents if a.lower() == to.lower()), to)
        if not AGENT_NAME_RE.match(to):
            raise HttpError(400, "to must be an agent's name")
    return {"id": reply_id, "messageId": message_id, "thread": thread, "title": title or None,
            "choice": choice or None, "text": text or None, "sentAt": sent_at,
            "attachments": attachments, "to": to, "choiceIds": choice_ids, "other": other}


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


def read_image(db_path, image_id):
    """(content type, length) of a picture in the relay's outbox, or None."""
    if not os.path.exists(db_path):
        return None
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        db.execute("PRAGMA busy_timeout=5000")
        return db.execute("SELECT type, bytes FROM images WHERE id = ?", (image_id,)).fetchone()
    except sqlite3.OperationalError as e:
        # A relay from before pictures.
        if "no such table" in str(e):
            return None
        raise
    finally:
        db.close()


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
    # With sessions on, the reply token itself stays off the public door.
    accept_reply_token = True
    session_ttl = 72 * 3600
    newest = None
    max_attachment = 10 << 20
    per_ip = None
    overall = None
    transcriber = None
    notifiers = {}   # agent name (None: every reply) -> webhook.Notifier
    owners = None    # agents.Owners
    agents = ()

    def authorized(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        bearer = auth[7:].strip()
        if bearer.startswith(session.PREFIX + "."):
            return session.valid(bearer, self.token, self.session_ttl,
                                 newest=self.newest.value if self.newest else 0)
        return self.accept_reply_token and hmac.compare_digest(bearer.encode(), self.token)

    def route(self, method):
        ip = self.client_ip()
        # Everything counts against the sender, junk included.
        if not self.per_ip.allow(ip) or not self.overall.allow("all"):
            raise HttpError(429, "slow down")
        url = urlsplit(self.path)
        attachment = re.fullmatch(r"/attachments/([^/]+)", url.path)
        image = re.fullmatch(r"/images/([^/]+)", url.path)
        if (method, url.path) == ("POST", "/replies"):
            handler = self.post_reply
        elif method == "PUT" and attachment:
            handler = lambda ip, _query: self.put_attachment(ip, attachment.group(1))
        elif (method, url.path) == ("GET", "/messages"):
            handler = self.get_messages
        elif method == "GET" and image:
            handler = lambda ip, _query: self.get_image(ip, image.group(1))
        elif (method, url.path) == ("GET", "/replies"):
            handler = self.get_replies
        elif (method, url.path) == ("GET", "/transcripts"):
            handler = self.get_transcripts
        elif (method, url.path) == ("GET", "/agents"):
            handler = lambda ip, _query: self.send_json(200, {"agents": list(self.agents)})
        else:
            raise HttpError(404, "not found")
        # Reject before reading a byte of the body.
        if not self.authorized():
            raise HttpError(401, "unauthorized")
        handler(ip, parse_qs(url.query))

    def put_attachment(self, ip, attachment_id):
        if not ATTACHMENT_ID_RE.match(attachment_id):
            raise HttpError(400, "attachment id must be a_ and 16 hex digits")
        content_type = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if content_type not in ATTACHMENT_TYPES:
            raise HttpError(415, "send one of " + ", ".join(ATTACHMENT_TYPES))
        stored = self.store.add_attachment(
            attachment_id, content_type, lambda f: self.read_body_to(f, self.max_attachment))
        log.info("attachment %s (%s) from %s (%s)", attachment_id, content_type, ip,
                 "stored" if stored else "duplicate")
        if stored and self.transcriber and content_type.startswith("audio/"):
            self.transcriber.submit(attachment_id)
        self.send_json(201 if stored else 200, {"ok": True})

    def wake(self, reply):
        """POST to the webhook of the agent this reply is for, and only theirs."""
        seq = self.store.seq_of(reply["id"])
        owner = self.owners.owner(reply) if self.owners else None
        for name in {None, owner}:
            if name in self.notifiers:
                self.notifiers[name].poke(seq)

    def post_reply(self, ip, _query):
        reply = validate(self.read_json(), self.store, self.agents, self.outbox_db)
        stored = self.store.add(reply)
        answered_at, second = self.store.answer_of(reply["id"])
        log.info("reply %s from %s (%s%s)", reply["id"], ip, "stored" if stored else "duplicate",
                 ", a second answer" if second else "")
        if stored and self.notifiers:
            self.wake(reply)
        body = {"ok": True}
        if answered_at:
            body.update(answeredAt=answered_at, secondAnswer=second)
        self.send_json(202, body)

    def get_messages(self, ip, query):
        after, limit = paging(query)
        messages = read_messages(self.outbox_db, after, limit)
        log.info("%d message(s) after %d to %s", len(messages), after, ip)
        self.send_json(200, {"messages": messages})

    def get_image(self, ip, image_id):
        """A picture from an agent's message, which the phone checks against the
        signed message's sha256 before showing it."""
        if not images.IMAGE_ID_RE.match(image_id):
            raise HttpError(400, "image id must be i_ and 16 hex digits")
        row = read_image(self.outbox_db, image_id)
        if not row:
            raise HttpError(404, "no such image; it may have expired")
        try:
            self.send_file(os.path.join(images.images_dir(self.outbox_db), image_id), row[0], row[1])
        except FileNotFoundError:
            raise HttpError(404, "no such image; it may have expired")
        log.info("image %s to %s", image_id, ip)

    def get_replies(self, ip, query):
        after, limit = paging(query)
        replies = self.store.since(after, limit)
        log.info("%d reply(s) after %d to %s", len(replies), after, ip)
        self.send_json(200, {"replies": replies})

    def get_transcripts(self, ip, query):
        after, limit = paging(query)
        transcripts = self.store.transcripts_since(after, limit)
        log.info("%d transcript(s) after %d to %s", len(transcripts), after, ip)
        self.send_json(200, {"transcripts": transcripts})


class SessionHandler(Handler):
    """The LAN-only door: trades proof of the reply token for a session token.

    Served over HTTPS on the LAN address, never through Funnel. The phone pins
    its certificate, and proves it holds the reply token without sending it.
    """

    token = b""
    session_ttl = 72 * 3600
    limit = None
    nonces = None
    newest = None

    def route(self, method):
        ip = self.client_ip()
        if not self.limit.allow(ip):
            raise HttpError(429, "slow down")
        if (method, urlsplit(self.path).path) != ("POST", "/session"):
            raise HttpError(404, "not found")
        data = self.read_json()
        at, nonce, proof = data.get("at"), data.get("nonce"), data.get("proof")
        if not isinstance(at, int) or isinstance(at, bool) or not isinstance(nonce, str) or not isinstance(proof, str):
            raise HttpError(400, "send at (unix seconds), nonce and proof")
        if abs(time.time() - at) > session.CLOCK_SKEW:
            raise HttpError(400, "clock is too far off")
        if not session.NONCE_RE.match(nonce):
            raise HttpError(400, "nonce must be 32-64 hex digits")
        if not hmac.compare_digest(proof, session.proof(self.token, at, nonce)):
            log.warning("session refused from %s: bad proof", ip)
            raise HttpError(401, "unauthorized")
        if not self.nonces.fresh(nonce):
            raise HttpError(401, "nonce already used")
        token, expires = session.issue(self.token, self.session_ttl)
        # This one retires every session before it.
        self.newest.set(expires)
        log.info("session for %s until %s", ip, datetime.fromtimestamp(expires, timezone.utc).isoformat())
        self.send_json(200, {"session": token,
                             "expiresAt": datetime.fromtimestamp(expires, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})


def serve_sessions(host, port, cert, key):
    server = serve(SessionHandler, host, port)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def notifiers(names, settings):
    """A webhook per agent that has one. The older single WEBHOOK_URL (with
    WEBHOOK_AGENT, the IP whose ack it watches) is the default agent's, unless
    that agent has its own; with no AGENTS at all, it fires for every reply."""
    cursor = env("WEBHOOK_CURSOR", "/outbox/cursor.json")
    reping = env_int("WEBHOOK_REPING_SECONDS", 900)
    out = {}
    for name, config in settings.items():
        if config["webhook_url"]:
            out[name] = webhook.Notifier(config["webhook_url"], config["webhook_key"], name, cursor,
                                         agents.cursor_key(name, config), reping)
    if env("WEBHOOK_URL"):
        default = names[0] if names else None
        if default not in out:
            out[default] = webhook.Notifier(env("WEBHOOK_URL"), env("WEBHOOK_KEY", required=True), default, cursor,
                                            env("WEBHOOK_AGENT") or (settings[default]["ip"] if default else None),
                                            reping)
    for name, notifier in out.items():
        notifier.start()
        log.info("replies %s wake a webhook%s", f"for {name}" if name else "(all)",
                 f" (re-sent while {notifier.agent} is behind)" if notifier.agent else "")
    return out


def main():
    setup_logging()
    token = env("REPLY_TOKEN", required=True)
    if len(token) < 32:
        raise SystemExit("REPLY_TOKEN is too short; use the one the phone tool generated")
    data_dir = env("DATA_DIR", "/data")

    InboxHandler.store = ReplyStore(f"{data_dir}/inbox.db", env("ATTACHMENTS_DIR"),
                                    env_int("ATTACHMENT_RETAIN_DAYS", 30),
                                    env_int("ATTACHMENT_MAX_TOTAL", 500 << 20))
    InboxHandler.outbox_db = env("OUTBOX_DB", "/outbox/outbox.db")
    InboxHandler.token = token.encode()
    InboxHandler.per_ip = RateLimiter(env_int("INBOX_RATE_PER_MIN", 20), 60)
    InboxHandler.overall = RateLimiter(env_int("INBOX_RATE_TOTAL_PER_MIN", 120), 60)
    InboxHandler.trusted_proxies = parse_ips(env("TRUSTED_PROXIES", "127.0.0.1,::1"))
    InboxHandler.client_ip_header = env("CLIENT_IP_HEADER", "X-Forwarded-For")
    InboxHandler.max_body = env_int("INBOX_MAX_BODY", 4096)
    InboxHandler.max_attachment = env_int("ATTACHMENT_MAX_BYTES", 10 << 20)

    InboxHandler.agents = tuple(parse_agents(env("AGENTS")))
    if InboxHandler.agents:
        log.info("conversations from the phone can go to %s", ", ".join(InboxHandler.agents))

    api_key = env("TRANSCRIBE_API_KEY")
    if api_key:
        InboxHandler.transcriber = transcribe.Transcriber(
            InboxHandler.store, api_key, env("TRANSCRIBE_URL", transcribe.DEFAULT_URL),
            env("TRANSCRIBE_MODEL", transcribe.DEFAULT_MODEL),
            keyterms_file=env("STT_KEYTERMS_FILE", f"{data_dir}/stt-keyterms.txt")).start()
        log.info("transcribing voice notes with %s at %s", InboxHandler.transcriber.model, InboxHandler.transcriber.url)
    else:
        log.info("no TRANSCRIBE_API_KEY: voice notes go through without transcripts")

    InboxHandler.notifiers = notifiers(InboxHandler.agents, agents.settings(InboxHandler.agents))
    InboxHandler.owners = agents.Owners(InboxHandler.agents, InboxHandler.outbox_db, f"{data_dir}/inbox.db")

    session_host = env("SESSION_HOST")
    ttl = env_int("SESSION_TTL_HOURS", 72) * 3600
    InboxHandler.session_ttl = SessionHandler.session_ttl = ttl
    # Sessions on means the reply token alone no longer opens the public door,
    # unless ACCEPT_REPLY_TOKEN says so (while the phone moves over).
    InboxHandler.accept_reply_token = env("ACCEPT_REPLY_TOKEN", "0" if session_host else "1") == "1"
    if session_host:
        SessionHandler.token = token.encode()
        SessionHandler.limit = RateLimiter(env_int("SESSION_RATE_PER_MIN", 10), 60)
        SessionHandler.nonces = session.Nonces()
        InboxHandler.newest = SessionHandler.newest = session.Newest(f"{data_dir}/session-newest.json")
        sessions = serve_sessions(session_host, env_int("SESSION_PORT", 18443),
                                  env("SESSION_CERT", "/session/cert.pem"), env("SESSION_KEY", "/session/key.pem"))
        threading.Thread(target=sessions.serve_forever, daemon=True).start()
        log.info("sessions of %dh on https://%s:%d; reply token %s on the public door", ttl // 3600,
                 session_host, sessions.server_address[1],
                 "still accepted" if InboxHandler.accept_reply_token else "not accepted")

    serve(InboxHandler, env("HOST", "0.0.0.0"), env_int("PORT", 8081)).serve_forever()


if __name__ == "__main__":
    main()
