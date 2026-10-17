"""Who each reply is for, and how each agent is known.

`AGENTS` names the agents the phone offers under To, the default first. Each can
have settings, read from the environment with its name in capitals and anything
other than letters and digits as `_` (Marcus Aurelius -> AGENT_MARCUS_AURELIUS_*):

    AGENT_<NAME>_TOKEN        a bearer token the agent sends to the relay
                              (Authorization: Bearer ...). Several agents can share
                              a tailnet IP, so the token is what tells them apart.
    AGENT_<NAME>_IP           a tailnet IP that is this agent without a token. The
                              agent's acknowledged position stays keyed by this IP.
    AGENT_<NAME>_WEBHOOK_URL  POSTed {"newest_seq": n, "to": "<name>"} by the inbox
    AGENT_<NAME>_WEBHOOK_KEY  when a reply for this agent arrives.

A reply is for:
  1. the agent it names (`to`, set on a conversation started on the phone), else
  2. the agent that sent the message it answers (recorded in the outbox), else
  3. for a phone conversation (t_...), the agent the conversation was started for,
  4. otherwise the default agent: the first in AGENTS. Messages sent before the
     outbox recorded senders belong to the default agent.
"""

import json
import os
import re
import sqlite3

from .common import log

MAX_AGENTS = 8
AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9 ._-]{1,24}$")


def parse_agents(value):
    """`AGENTS`: the names the phone offers under To, the default first."""
    names = [n.strip() for n in (value or "").split(",") if n.strip()]
    bad = [n for n in names if not AGENT_NAME_RE.match(n)]
    if bad or len(names) > MAX_AGENTS or len({n.lower() for n in names}) != len(names):
        raise SystemExit(f"AGENTS must be up to {MAX_AGENTS} distinct names of [A-Za-z0-9 ._-], at most 24 chars each")
    return names


def slug(name):
    return re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")


def settings(names, environ=None):
    environ = os.environ if environ is None else environ
    out = {}
    if len({slug(n) for n in names}) != len(names):
        raise SystemExit("two AGENTS names give the same AGENT_<NAME>_ prefix; make them differ in letters or digits")
    for name in names:
        prefix = f"AGENT_{slug(name)}_"
        get = lambda k: (environ.get(prefix + k) or "").strip() or None
        out[name] = {"token": get("TOKEN"), "ip": get("IP"),
                     "webhook_url": get("WEBHOOK_URL"), "webhook_key": get("WEBHOOK_KEY")}
        if out[name]["webhook_url"] and not out[name]["webhook_key"]:
            raise SystemExit(f"{prefix}WEBHOOK_URL is set without {prefix}WEBHOOK_KEY")
        if out[name]["token"] and out[name]["token"] in [c["token"] for n, c in out.items() if n != name]:
            raise SystemExit(f"{prefix}TOKEN is the same as another agent's")
        if out[name]["token"] and len(out[name]["token"]) < 32:
            raise SystemExit(f"{prefix}TOKEN is too short; use 32 or more random characters")
    return out


def cursor_key(name, config):
    """Where an agent's acknowledged position is kept in cursor.json."""
    return config.get("ip") or f"agent:{name}"


def query(path, sql, args):
    """Rows from a database this side may only read. Missing file, table or
    column (an older version) reads as no rows."""
    if not path or not os.path.exists(path):
        return []
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        db.execute("PRAGMA busy_timeout=5000")
        return db.execute(sql, args).fetchall()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e) or "no such column" in str(e):
            return []
        raise
    finally:
        db.close()


class Owners:
    """Decides who a reply is for, from the outbox (who sent what) and the inbox
    (who a phone conversation was started for)."""

    def __init__(self, names, outbox_db, inbox_db):
        self.names = tuple(names)
        self.outbox_db = outbox_db
        self.inbox_db = inbox_db

    @property
    def default(self):
        return self.names[0] if self.names else None

    def canonical(self, name):
        return next((a for a in self.names if a.lower() == name.lower()), name)

    def message_agent(self, message_id, depth=0):
        rows = query(self.outbox_db, "SELECT agent, payload FROM messages WHERE id = ?", (message_id,))
        if not rows:
            return None
        agent, payload = rows[0]
        if agent:
            return agent
        try:
            thread = json.loads(payload).get("thread")
        except ValueError:
            thread = None
        if thread and thread != message_id and depth < 3:
            return self.thread_agent(thread, depth + 1)
        return None

    def thread_agent(self, thread, depth=0):
        if thread.startswith("t_"):
            rows = query(self.inbox_db, "SELECT to_agent FROM replies WHERE thread = ? AND to_agent IS NOT NULL"
                         " ORDER BY seq LIMIT 1", (thread,))
            return rows[0][0] if rows else None
        return self.message_agent(thread, depth)

    def owner(self, reply):
        try:
            if reply.get("to"):
                return self.canonical(reply["to"])
            for ref, lookup in ((reply.get("messageId"), self.message_agent), (reply.get("thread"), self.thread_agent)):
                if ref:
                    found = lookup(ref)
                    if found:
                        return self.canonical(found)
        except sqlite3.Error as e:
            log.warning("couldn't tell who reply %s is for: %s", reply.get("id"), e)
        return self.default
