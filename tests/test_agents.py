"""Several named agents: each reads, acknowledges and is woken for only its own replies."""

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from lightrelay import agents, inbox, relay, webhook
from lightrelay.common import RateLimiter
from tests import test_end_to_end as e2e

AGENT_IP, request = e2e.AGENT_IP, e2e.request

NAMES = ("Huginn", "Jarvis", "Marcus Aurelius")
JARVIS = "j" * 40
MARCUS = "m" * 40
ENV = {
    "AGENT_HUGINN_IP": AGENT_IP,             # Huginn, as before: known by its IP, no token
    "AGENT_JARVIS_TOKEN": JARVIS,
    "AGENT_MARCUS_AURELIUS_TOKEN": MARCUS,
}


class Hooks(BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Hooks.received.append((self.path, self.headers["Authorization"], body))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


class Agents(unittest.TestCase):
    reply = e2e.EndToEnd.reply
    fetch = e2e.EndToEnd.fetch

    def setUp(self):
        e2e.EndToEnd.setUp(self)
        self.addCleanup(e2e.EndToEnd.tearDown, self)
        inbox.InboxHandler.per_ip = RateLimiter(100, 60)
        Hooks.received = []
        self.hooks = HTTPServer(("127.0.0.1", 0), Hooks)
        threading.Thread(target=self.hooks.serve_forever, daemon=True).start()
        self.addCleanup(self.hooks.server_close)
        self.addCleanup(self.hooks.shutdown)
        base = f"http://127.0.0.1:{self.hooks.server_address[1]}"
        env = dict(ENV, AGENT_HUGINN_WEBHOOK_URL=base + "/huginn", AGENT_HUGINN_WEBHOOK_KEY="hk",
                   AGENT_JARVIS_WEBHOOK_URL=base + "/jarvis", AGENT_JARVIS_WEBHOOK_KEY="jk")
        settings = agents.settings(NAMES, env)
        owners = agents.Owners(NAMES, inbox.InboxHandler.outbox_db, relay.RelayHandler.inbox_db)
        for cls, attrs in ((relay.RelayHandler, {"agent_settings": settings, "owners": owners}),
                           (inbox.InboxHandler, {"agents": NAMES, "owners": owners})):
            for k, v in attrs.items():
                self.addCleanup(setattr, cls, k, getattr(cls, k))
                setattr(cls, k, v)
        notifiers = {n: webhook.Notifier(c["webhook_url"], c["webhook_key"], n, check_every=0.05,
                                         backoff=(0.05, 0.1)).start()
                     for n, c in settings.items() if c["webhook_url"]}
        self.addCleanup(setattr, inbox.InboxHandler, "notifiers", {})
        inbox.InboxHandler.notifiers = notifiers

    def as_agent(self, path, method="GET", body=None, token=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return request(self.relay_url + path, method, body, headers, via=AGENT_IP)

    def mine(self, token=None):
        status, body = self.as_agent("/replies", token=token)
        self.assertEqual(status, 200)
        return [r["id"] for r in body["replies"]]

    def hooks_after(self, n):
        deadline = time.time() + 5
        while len(Hooks.received) < n and time.time() < deadline:
            time.sleep(0.02)
        time.sleep(0.2)
        return Hooks.received

    def test_each_agent_reads_only_its_own(self):
        # Jarvis sends a message; the phone answers it.
        m = self.as_agent("/notify", "POST", {"headline": "Groceries?"}, token=JARVIS)[1]["id"]
        self.reply({"id": "r_jarvis", "messageId": m, "text": "Milk"})
        # A conversation started on the phone for Marcus Aurelius, and its follow-up.
        t = "t_00000000000000aa"
        self.reply({"id": "r_marcus", "thread": t, "title": "Stoicism", "text": "Hi", "to": "marcus aurelius"})
        self.reply({"id": "r_marcus2", "thread": t, "text": "More", "to": "Marcus Aurelius"})
        # Marcus answers in that conversation; the phone replies to the answer.
        answer = self.as_agent("/notify", "POST", {"headline": "Hello", "thread": t}, token=MARCUS)[1]["id"]
        self.reply({"id": "r_marcus3", "messageId": answer, "thread": t, "text": "Thanks"})
        # Answers to messages from before senders were recorded, and phone starts
        # without a recipient, are the default agent's (Huginn, first in AGENTS).
        self.reply({"id": "r_old", "messageId": "m_00000000000000ff", "choice": "Yes"})
        self.reply({"id": "r_nobody", "thread": "t_00000000000000bb", "text": "Hey"})

        self.assertEqual(self.mine(JARVIS), ["r_jarvis"])
        self.assertEqual(self.mine(MARCUS), ["r_marcus", "r_marcus2", "r_marcus3"])
        self.assertEqual(self.mine(), ["r_old", "r_nobody"])           # Huginn, by IP
        self.assertEqual(self.as_agent("/replies", token=JARVIS)[1]["agent"], "Jarvis")

    def test_each_agent_acknowledges_for_itself(self):
        m = self.as_agent("/notify", "POST", {"headline": "Q"}, token=JARVIS)[1]["id"]
        self.reply({"id": "r1", "messageId": m, "text": "A"})
        self.reply({"id": "r2", "thread": "t_00000000000000aa", "text": "B", "to": "Huginn"})
        self.assertEqual(self.as_agent("/replies/ack", "POST", {"upTo": 1}, token=JARVIS)[1], {"cursor": 1})
        self.assertEqual(self.mine(JARVIS), [])
        self.assertEqual(self.mine(), ["r2"])
        with open(relay.RelayHandler.cursor.path) as f:
            # Huginn's position stays under its IP; Jarvis gets its own key.
            self.assertEqual(json.load(f)["agents"], {"agent:Jarvis": 1})

    def test_a_named_agents_message_says_who_sent_it(self):
        self.as_agent("/notify", "POST", {"headline": "Hi"}, token=MARCUS)
        self.as_agent("/notify", "POST", {"headline": "Hello"})                 # Huginn, by IP
        payloads = [json.loads(b.decode().split(".", 2)[2]) for b in e2e.FakePush.received]
        self.assertEqual([p.get("from") for p in payloads], ["Marcus Aurelius", "Huginn"])

    def test_unknown_token_is_refused(self):
        self.assertEqual(self.as_agent("/replies", token="x" * 40)[0], 401)

    def test_a_reply_wakes_only_its_agents_webhook(self):
        m = self.as_agent("/notify", "POST", {"headline": "Q"}, token=JARVIS)[1]["id"]
        self.reply({"id": "r1", "messageId": m, "text": "A"})
        self.reply({"id": "r2", "thread": "t_00000000000000aa", "text": "B", "to": "Huginn"})
        # Marcus Aurelius has no webhook: nobody is woken for this one.
        self.reply({"id": "r3", "thread": "t_00000000000000cc", "text": "C", "to": "Marcus Aurelius"})
        got = sorted(self.hooks_after(2))
        self.assertEqual(got, [("/huginn", "Bearer hk", {"newest_seq": 2, "to": "Huginn"}),
                               ("/jarvis", "Bearer jk", {"newest_seq": 1, "to": "Jarvis"})])


class Settings(unittest.TestCase):
    def test_names_become_prefixes(self):
        self.assertEqual(agents.slug("Marcus Aurelius"), "MARCUS_AURELIUS")
        self.assertEqual(agents.slug("Real Estate"), "REAL_ESTATE")
        got = agents.settings(["Real Estate"], {"AGENT_REAL_ESTATE_WEBHOOK_URL": "https://x", "AGENT_REAL_ESTATE_WEBHOOK_KEY": "k"})
        self.assertEqual(got["Real Estate"]["webhook_url"], "https://x")
        self.assertEqual(agents.cursor_key("Real Estate", got["Real Estate"]), "agent:Real Estate")

    def test_bad_settings_stop_the_start(self):
        for names, env in ((["A"], {"AGENT_A_WEBHOOK_URL": "https://x"}),          # no key
                           (["A"], {"AGENT_A_TOKEN": "short"}),
                           (["A", "B"], {"AGENT_A_TOKEN": "t" * 40, "AGENT_B_TOKEN": "t" * 40}),
                           (["Real Estate", "Real-Estate"], {})):
            with self.assertRaises(SystemExit):
                agents.settings(names, env)

    def test_seven_bots_fit(self):
        names = inbox.parse_agents("Huginn,Jarvis,Othala,Luna,Marcus Aurelius,Real Estate,Shopping")
        self.assertEqual(len(agents.settings(names, {})), 7)


class LegacyWebhook(unittest.TestCase):
    def test_the_single_webhook_is_the_default_agents(self):
        import os
        old = {k: os.environ.get(k) for k in ("WEBHOOK_URL", "WEBHOOK_KEY", "WEBHOOK_AGENT")}
        os.environ.update(WEBHOOK_URL="http://127.0.0.1:9/x", WEBHOOK_KEY="k", WEBHOOK_AGENT="100.83.6.74")
        try:
            got = inbox.notifiers(("Huginn", "Claude"), agents.settings(("Huginn", "Claude"), {}))
            self.assertEqual(list(got), ["Huginn"])
            self.assertEqual((got["Huginn"].name, got["Huginn"].agent), ("Huginn", "100.83.6.74"))
            self.assertEqual(list(inbox.notifiers((), {})), [None])
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
