"""The relay: tailnet-only, published with Tailscale Serve, never Funnel.

The agent sends it messages for the phone (POST /notify) and collects replies
from it (GET /replies, POST /replies/ack), and the voice notes and photos they
carry (GET /attachments/<id>). A message can carry a picture for the phone
(see images.py), which the agent can read back at GET /images/<id>. Every route but /healthz accepts
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
from email.parser import BytesParser
from email.policy import HTTP
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from . import agents, images
from .agents import parse_agents
from .common import (message_options, ATTACHMENT_ID_RE, REPLY_COLUMNS, HttpError, Handler, RateLimiter, attachment_ids, env,
                     env_int, log, parse_ips, reply_from_row, serve, setup_logging, with_transcripts)

MAX_HEADLINE = 120
MAX_DETAIL = 1000
MAX_CHOICES = 4
MAX_CHOICE = 24
CHOICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,24}$")
CHOICE_STYLES = ("default", "danger")
REF_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
RETAIN_DAYS = 90
# How many replies one /replies call looks through for a named agent's own.
SCAN_LIMIT = 5000
MAX_ROWS = 5000
# GET /channel: entries returned by default, and at most.
CHANNEL_LIMIT = 100
MAX_CHANNEL_LIMIT = 1000

OUTBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    seq     INTEGER PRIMARY KEY AUTOINCREMENT,
    id      TEXT NOT NULL UNIQUE,
    at      TEXT NOT NULL,
    payload TEXT NOT NULL,
    body    TEXT NOT NULL,
    agent   TEXT
);
"""


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_multipart(content_type, body):
    """multipart/form-data, as `curl -F` sends it: text fields headline, detail,
    ref and choice (repeated, or `choices` as a JSON list), and the
    picture as the file field `image`. Returns (fields, image bytes or None)."""
    try:
        msg = BytesParser(policy=HTTP).parsebytes(
            b"Content-Type: " + content_type.encode("latin-1") + b"\r\n\r\n" + body)
    except (UnicodeEncodeError, ValueError):
        raise HttpError(400, "bad multipart body")
    if not msg.is_multipart():
        raise HttpError(400, "bad multipart body")
    fields, choices, image = {}, [], None
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        payload = part.get_payload(decode=True) or b""
        if name == "image":
            if image is not None:
                raise HttpError(400, "one image per message")
            image = payload
            continue
        try:
            value = payload.decode("utf-8")
        except UnicodeDecodeError:
            raise HttpError(400, f"{name} must be UTF-8")
        if name == "choice":
            choices.append(value)
        elif name == "choices":
            try:
                choices.extend(json.loads(value))
            except (ValueError, TypeError):
                raise HttpError(400, "choices must be a JSON list")
        elif name in ("headline", "detail", "ref", "select", "other"):
            fields[name] = value
    fields["choices"] = choices
    if image is not None and not image:
        raise HttpError(400, "image is empty")
    return fields, image


def parse_message(content_type, body):
    """A message is plain text (first line headline, the rest detail), JSON, or
    multipart/form-data. JSON and multipart can carry a PNG or JPEG: JSON as
    `image`, base64 or a data: URL. Returns (message, image bytes or None); the
    image is checked and shrunk later (images.prepare), once the rate limit allows."""
    image = None
    if content_type.startswith("multipart/form-data"):
        data, image = parse_multipart(content_type, body)
        headline, detail, choices = data.get("headline"), data.get("detail", ""), data["choices"]
        ref = data.get("ref") or None
        other = data.get("other")
        if other is not None:
            other = other.strip().lower() in ("1", "true", "yes", "on")
        return _message(headline, detail, choices, ref, data.get("select") or None, other), image
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
        # A `thread` from before one conversation per agent is ignored: the sender is the conversation.
        ref = data.get("ref")
        select, other = data.get("select"), data.get("other")
        if data.get("image") is not None:
            image = images.decode_base64(data["image"])
    else:
        headline, _, detail = raw.strip().partition("\n")
        choices, ref, select, other = [], None, None, None
    return _message(headline, detail, choices, ref, select, other), image


def _options(choices):
    """Choices are labels, or objects {id, label, style}. Returns [{id, label, style}];
    ids default to c1, c2, ... and style to "default"."""
    error = (f"choices must be up to {MAX_CHOICES} labels of at most {MAX_CHOICE} chars,"
             ' or {"id", "label", "style": "default"|"danger"} objects')
    if not isinstance(choices, list) or len(choices) > MAX_CHOICES:
        raise HttpError(400, error)
    options = []
    for n, c in enumerate(choices, 1):
        if isinstance(c, str):
            c = {"label": c}
        if not isinstance(c, dict) or set(c) - {"id", "label", "style"}:
            raise HttpError(400, error)
        label, cid, style = c.get("label"), c.get("id", f"c{n}"), c.get("style") or "default"
        if not isinstance(label, str) or not label.strip() or len(label.strip()) > MAX_CHOICE:
            raise HttpError(400, error)
        if not isinstance(cid, str) or not CHOICE_ID_RE.match(cid):
            raise HttpError(400, "a choice id must be 1-24 of [A-Za-z0-9_-]")
        if style not in CHOICE_STYLES:
            raise HttpError(400, 'a choice style must be "default" or "danger"')
        options.append({"id": cid, "label": label.strip(), "style": style})
    if len({o["id"] for o in options}) != len(options):
        raise HttpError(400, "choice ids must be distinct")
    if len({o["label"].lower() for o in options}) != len(options):
        raise HttpError(400, "choice labels must be distinct")
    return options


def _message(headline, detail, choices, ref, select=None, other=None):
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
    options = _options(choices)
    if select not in (None, "one", "many"):
        raise HttpError(400, 'select must be "one" or "many"')
    if other is not None and not isinstance(other, bool):
        raise HttpError(400, "other must be true or false")
    if (select == "many" or other) and not options:
        raise HttpError(400, "select and other need choices")
    if select == "many" and len(options) < 2:
        raise HttpError(400, 'select "many" needs at least 2 choices')
    if ref is not None and (not isinstance(ref, str) or not REF_RE.match(ref)):
        raise HttpError(400, "ref must be 1-64 of [A-Za-z0-9_.:-]")

    message = {
        "v": 1,
        "id": "m_" + secrets.token_hex(8),
        "at": now_iso(),
        "headline": headline,
        "detail": detail,
        # Labels only, so a tool from before choice objects still shows them.
        "choices": [o["label"] for o in options],
    }
    # The richer form, only when it says more than the labels do.
    if any(o["style"] != "default" or o["id"] != f"c{n}" for n, o in enumerate(options, 1)):
        message["options"] = options
    if select == "many":
        message["select"] = "many"
    if other:
        message["other"] = True
    if ref:
        message["ref"] = ref
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
        self.db.executescript(images.IMAGES_SCHEMA)
        self.images_dir = images.images_dir(path)
        # Which agent sent each message, so a reply to it goes back to them.
        if "agent" not in {r[1] for r in self.db.execute("PRAGMA table_info(messages)")}:
            self.db.execute("ALTER TABLE messages ADD COLUMN agent TEXT")
        self.lock = threading.Lock()

    def add(self, message, body, agent=None):
        payload = body.decode().split(".", 2)[2]
        image = message.get("image")
        with self.lock:
            if image:
                self.db.execute(
                    "INSERT INTO images (id, message_id, type, bytes, width, height, sha256, at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (image["id"], message["id"], image["type"], image["bytes"], image["width"],
                     image["height"], image["sha256"], message["at"]))
            self.db.execute("INSERT INTO messages (id, at, payload, body, agent) VALUES (?, ?, ?, ?, ?)",
                            (message["id"], message["at"], payload, body.decode(), agent))
            self._prune()

    def image(self, image_id):
        """(content type, length) of a kept picture, or None."""
        with self.lock:
            return self.db.execute("SELECT type, bytes FROM images WHERE id = ?", (image_id,)).fetchone()

    def all(self):
        """(seq, message) for everything kept, oldest first."""
        with self.lock:
            rows = self.db.execute("SELECT seq, payload FROM messages ORDER BY seq").fetchall()
        return [(seq, json.loads(payload)) for seq, payload in rows]

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
        # A picture goes with its message; a file without a row is left from a failed send.
        gone = [r[0] for r in self.db.execute(
            "SELECT id FROM images WHERE message_id NOT IN (SELECT id FROM messages)")]
        if gone:
            self.db.executemany("DELETE FROM images WHERE id = ?", [(i,) for i in gone])
        if os.path.isdir(self.images_dir):
            kept = {r[0] for r in self.db.execute("SELECT id FROM images")}
            cutoff = time.time() - 3600
            gone += [f for f in os.listdir(self.images_dir) if f not in kept
                     and os.path.getmtime(os.path.join(self.images_dir, f)) < cutoff]
        images.remove(self.images_dir, gone)


class Cursor:
    """How far through the inbox each agent has acknowledged, by tailnet IP. Every
    agent sees every reply and acknowledges only for itself, so one that is
    rarely running never holds back another. Lives in the relay's own data dir:
    the inbox database is mounted read-only here.

    The file is `{"seq": n, "agents": {"<ip>": n}}`. `seq` is the one position
    shared before agents had their own; an agent that hasn't acknowledged
    anything yet starts there, so the switch re-delivers nothing.
    """

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()

    def _load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
            return int(data.get("seq", 0)), {k: int(v) for k, v in data.get("agents", {}).items()}
        except (FileNotFoundError, ValueError, KeyError, AttributeError):
            return 0, {}

    def get(self, agent):
        shared, agents = self._load()
        return agents.get(agent, shared)

    def advance(self, agent, seq):
        with self.lock:
            shared, agents = self._load()
            agents[agent] = max(seq, agents.get(agent, shared))
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"seq": shared, "agents": agents}, f)
            os.replace(tmp, self.path)
            return agents[agent]


def read_inbox(db_path, sql, args):
    """Rows from the inbox's database, which this side mounts read-only."""
    if not os.path.exists(db_path):
        return []
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        db.execute("PRAGMA busy_timeout=5000")
        return db.execute(sql, args).fetchall()
    except sqlite3.OperationalError as e:
        # An older inbox, from before the table or column asked for.
        if "no such table" in str(e) or "no such column" in str(e):
            return []
        raise
    finally:
        db.close()


def read_replies(db_path, after, limit):
    rows = read_inbox(db_path, f"SELECT {REPLY_COLUMNS} FROM replies WHERE seq > ? ORDER BY seq LIMIT ?",
                      (after, limit))
    replies = [reply_from_row(r) for r in rows]
    ids = attachment_ids(replies)
    # A separate read: an inbox from before transcripts has no such table, and that
    # mustn't hide the replies.
    transcripts = read_inbox(db_path, "SELECT attachment_id, text, error FROM transcripts WHERE attachment_id IN"
                             f" ({','.join('?' * len(ids))})", ids) if ids else []
    return with_transcripts(replies, {r[0]: (r[1], r[2]) for r in transcripts})


class RelayHandler(Handler):
    allowed_peers = frozenset()
    push_endpoint = None
    push_key = None
    push_timeout = 15
    notify_limit = None
    outbox = None
    inbox_db = None
    attachments_dir = None
    cursor = None
    agent_settings = {}   # name -> settings (agents.settings)
    owners = None         # agents.Owners
    # POST /notify with a picture (JSON or multipart). Others keep max_body.
    max_notify_body = images.Limits.max_upload * 4 // 3 + 65536

    def identify(self, ip):
        """(agent name or None, cursor key). A bearer token names the agent; else a
        configured AGENT_<NAME>_IP; else the caller is unnamed and keyed by IP,
        seeing every reply, as before agents had names."""
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:].strip().encode()
            for name, config in self.agent_settings.items():
                if config["token"] and hmac.compare_digest(token, config["token"].encode()):
                    return name, agents.cursor_key(name, config)
            raise HttpError(401, "unknown agent token")
        for name, config in self.agent_settings.items():
            if config["ip"] == str(ip):
                return name, agents.cursor_key(name, config)
        return None, str(ip)

    def route(self, method):
        url = urlsplit(self.path)
        if method == "GET" and url.path == "/healthz":
            return self.send_json(200, {"ok": True, "paired": bool(self.push_key), "push": bool(self.push_endpoint)})

        ip = self.client_ip()
        if ip not in self.allowed_peers:
            log.warning("refused %s %s from %s", method, url.path, ip)
            raise HttpError(403, "forbidden")

        name, key = self.identify(ip)
        if method == "POST" and url.path == "/notify":
            return self.notify(name)
        if method == "GET" and url.path == "/replies":
            return self.replies(key, parse_qs(url.query), name)
        if method == "GET" and url.path == "/channel":
            return self.channel(name, parse_qs(url.query))
        if method == "POST" and url.path == "/replies/ack":
            return self.ack(key)
        if method == "GET" and url.path.startswith("/attachments/"):
            return self.attachment(url.path[len("/attachments/"):])
        if method == "GET" and url.path.startswith("/images/"):
            return self.image(url.path[len("/images/"):])
        raise HttpError(404, "not found")

    def notify(self, agent=None):
        if not self.push_key:
            raise HttpError(503, "not paired: set PUSH_KEY")
        content_type = self.headers.get("Content-Type", "text/plain")
        # Only a body that can carry a picture may be big.
        big = content_type.startswith(("application/json", "multipart/form-data"))
        message, image = parse_message(content_type, self.read_body(self.max_notify_body if big else None))
        if not self.notify_limit.allow("notify"):
            raise HttpError(429, "notify rate limit reached")
        if image is not None:
            data, image_type, width, height = images.prepare(image)
            image_id, digest = images.store(self.outbox.images_dir, data)
            message["image"] = {"id": image_id, "type": image_type, "bytes": len(data),
                                "width": width, "height": height, "sha256": digest}
            log.info("image %s for %s: %d bytes in, %dx%d %s of %d bytes kept", image_id, message["id"],
                     len(image), width, height, image_type, len(data))
        if agent:
            # Signed with the rest, so the phone can file it in that agent's channel.
            message["from"] = agent

        body = sign(message, self.push_key)
        self.outbox.add(message, body, agent)
        pushed = self.push(message, body) if self.push_endpoint else False
        log.info("stored %s (%s)", message["id"], "pushed" if pushed else "waiting to be fetched")
        result = {"id": message["id"], "at": message["at"], "pushed": pushed}
        if "image" in message:
            result["image"] = dict(message["image"], path=f"/images/{message['image']['id']}")
        self.send_json(202, result)

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

    def replies(self, key, query, name=None):
        try:
            limit = min(max(int(query.get("limit", ["50"])[0]), 1), 200)
            after = int(query["after"][0]) if "after" in query else self.cursor.get(key)
        except ValueError:
            raise HttpError(400, "after and limit must be integers")
        if name is None:
            rows = read_replies(self.inbox_db, after, limit)
            return self.send_json(200, {"replies": rows, "cursor": self.cursor.get(key)})
        # A named agent reads only the replies that are for it.
        rows, scanned = [], 0
        while len(rows) < limit and scanned < SCAN_LIMIT:
            page = read_replies(self.inbox_db, after, 200)
            if not page:
                break
            scanned += len(page)
            after = page[-1]["seq"]
            rows += [r for r in page if self.owners.owner(r) == name]
        self.send_json(200, {"replies": rows[:limit], "cursor": self.cursor.get(key), "agent": name})

    def channel(self, name, query):
        """Everything between the phone and one agent, as the phone shows it: the
        agent's messages and the replies that are for it, oldest first. Read-only:
        it neither acknowledges nor moves anyone's cursor."""
        if name is None:
            raise HttpError(403, "a channel belongs to a named agent: send your token")
        try:
            limit = min(max(int(query.get("limit", [str(CHANNEL_LIMIT)])[0]), 1), MAX_CHANNEL_LIMIT)
        except ValueError:
            raise HttpError(400, "limit must be an integer")
        owners = self.owners.cached()
        entries = []
        for seq, m in self.outbox.all():
            if owners.message_owner(m["id"]) != name:
                continue
            entry = {"direction": "out", "kind": "message", "seq": seq, "at": m["at"], "id": m["id"],
                     "headline": m.get("headline", ""),
                     "detail": m.get("detail", ""), "choices": m.get("choices", [])}
            if m.get("choices"):
                entry["options"] = message_options(m)
                entry["select"] = m.get("select", "one")
                entry["other"] = bool(m.get("other"))
            if m.get("ref"):
                entry["ref"] = m["ref"]
            if m.get("image"):
                entry["image"] = dict(m["image"], path=f"/images/{m['image']['id']}")
            entries.append(entry)
        for r in read_replies(self.inbox_db, 0, MAX_ROWS):
            if owners.owner(r) != name:
                continue
            for a in r["attachments"]:
                a["path"] = f"/attachments/{a['id']}"
            entries.append({"direction": "in", "kind": "reply", "seq": r["seq"], "at": r["receivedAt"],
                            "id": r["id"], "messageId": r["messageId"], "to": r["to"],
                            "choice": r["choice"], "text": r["text"],
                            "choiceIds": r["choiceIds"], "other": r["other"], "answeredAt": r["answeredAt"],
                            "secondAnswer": r["secondAnswer"],
                            "attachments": r["attachments"], "sentAt": r["sentAt"], "receivedAt": r["receivedAt"]})
        # One clock for both sides: when the relay stored the message or received the reply.
        # Times are to the second; within one, the agent's messages come first (a reply
        # can't come before the message it answers), then each side in its own order.
        entries.sort(key=lambda e: (e["at"], e["direction"] == "in", e["seq"]))
        self.send_json(200, {"agent": name, "total": len(entries), "entries": entries[-limit:]})

    def attachment(self, attachment_id):
        if not ATTACHMENT_ID_RE.match(attachment_id):
            raise HttpError(400, "attachment id must be a_ and 16 hex digits")
        rows = read_inbox(self.inbox_db, "SELECT type, bytes FROM attachments WHERE id = ?", (attachment_id,))
        if not rows:
            raise HttpError(404, "no such attachment; it may have expired")
        content_type, length = rows[0]
        try:
            self.send_file(os.path.join(self.attachments_dir, attachment_id), content_type, length)
        except FileNotFoundError:
            # Pruned between the lookup and the open.
            raise HttpError(404, "no such attachment; it may have expired")

    def image(self, image_id):
        """A picture an agent sent with a message, as the phone gets it."""
        if not images.IMAGE_ID_RE.match(image_id):
            raise HttpError(400, "image id must be i_ and 16 hex digits")
        row = self.outbox.image(image_id)
        if not row:
            raise HttpError(404, "no such image; it may have expired")
        try:
            self.send_file(os.path.join(self.outbox.images_dir, image_id), row[0], row[1])
        except FileNotFoundError:
            raise HttpError(404, "no such image; it may have expired")

    def ack(self, agent):
        data = self.read_json()
        up_to = data.get("upTo")
        if not isinstance(up_to, int) or isinstance(up_to, bool) or up_to < 0:
            raise HttpError(400, "upTo must be a non-negative integer (a reply's seq)")
        self.send_json(200, {"cursor": self.cursor.advance(agent, up_to)})


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
    RelayHandler.attachments_dir = env(
        "ATTACHMENTS_DIR", os.path.join(os.path.dirname(RelayHandler.inbox_db), "attachments"))
    RelayHandler.cursor = Cursor(f"{data_dir}/cursor.json")
    names = parse_agents(env("AGENTS"))
    RelayHandler.agent_settings = agents.settings(names)
    RelayHandler.owners = agents.Owners(names, env("OUTBOX_DB", f"{data_dir}/outbox.db"), RelayHandler.inbox_db)
    if names:
        known = [f"{n} ({'token' if c['token'] else ''}{', ' if c['token'] and c['ip'] else ''}{'IP ' + c['ip'] if c['ip'] else ''})"
                 for n, c in RelayHandler.agent_settings.items() if c["token"] or c["ip"]]
        log.info("agents: %s; known by token or IP: %s", ", ".join(names), ", ".join(known) or "none")
    RelayHandler.trusted_proxies = parse_ips(env("TRUSTED_PROXIES", "127.0.0.1,::1"))
    RelayHandler.client_ip_header = env("CLIENT_IP_HEADER", "X-Forwarded-For")
    RelayHandler.max_body = env_int("RELAY_MAX_BODY", 8192)
    images.Limits.max_upload = env_int("IMAGE_MAX_BYTES", images.Limits.max_upload)
    images.Limits.max_pixels = env_int("IMAGE_MAX_PIXELS", images.Limits.max_pixels)
    images.Limits.max_width = env_int("IMAGE_MAX_WIDTH", images.Limits.max_width)
    images.Limits.max_height = env_int("IMAGE_MAX_HEIGHT", images.Limits.max_height)
    images.Limits.target_bytes = env_int("IMAGE_TARGET_BYTES", images.Limits.target_bytes)
    # base64 is 4/3 the size, plus room for the text.
    RelayHandler.max_notify_body = images.Limits.max_upload * 4 // 3 + 65536

    if not RelayHandler.push_key:
        log.warning("not paired yet: /notify will answer 503 until PUSH_KEY is set")
    elif not RelayHandler.push_endpoint:
        log.info("no PUSH_ENDPOINT: messages wait in the outbox for the tool to fetch")
    log.info("allowed peers: %s", ", ".join(sorted(map(str, allowed))))

    serve(RelayHandler, env("HOST", "0.0.0.0"), env_int("PORT", 8080)).serve_forever()


if __name__ == "__main__":
    main()
