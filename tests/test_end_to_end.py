"""The whole loop on loopback: agent -> relay -> fake push endpoint,
agent -> relay -> outbox -> inbox -> phone, and phone -> inbox -> relay -> agent. Run with `python -m unittest`."""

import hashlib
import hmac
import ipaddress
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

from lightrelay import inbox, relay
from lightrelay.common import RateLimiter, serve

AGENT_IP = "100.64.0.7"
STRANGER_IP = "100.64.0.99"
PUSH_KEY = b"k" * 43
REPLY_TOKEN = "t" * 43


class FakePush(BaseHTTPRequestHandler):
    received = []
    status = 200

    def do_POST(self):
        self.received.append(self.rfile.read(int(self.headers["Content-Length"])))
        self.send_response(self.status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


def start(handler):
    server = serve(handler, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def request(url, method="GET", body=None, headers=None, via=None):
    headers = dict(headers or {})
    if via:
        headers["X-Forwarded-For"] = via
    if isinstance(body, (dict, list)):
        body = json.dumps(body).encode()
        headers.setdefault("Content-Type", "application/json")
    elif isinstance(body, str):
        body = body.encode()
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as e:
        with e:
            return e.code, json.loads(e.read() or b"{}")


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        FakePush.received = []
        FakePush.status = 200
        self.push, push_url = start(FakePush)

        loopback = frozenset({ipaddress.ip_address("127.0.0.1")})

        outbox_db = f"{self.tmp.name}/outbox.db"
        inbox.InboxHandler.store = inbox.ReplyStore(f"{self.tmp.name}/inbox.db")
        inbox.InboxHandler.outbox_db = outbox_db
        inbox.InboxHandler.token = REPLY_TOKEN.encode()
        inbox.InboxHandler.per_ip = RateLimiter(5, 60)
        inbox.InboxHandler.overall = RateLimiter(1000, 60)
        inbox.InboxHandler.trusted_proxies = loopback
        self.inbox, self.inbox_url = start(inbox.InboxHandler)

        relay.RelayHandler.allowed_peers = frozenset({ipaddress.ip_address(AGENT_IP)})
        relay.RelayHandler.push_endpoint = push_url + "/push/abc"
        relay.RelayHandler.push_key = PUSH_KEY
        relay.RelayHandler.notify_limit = RateLimiter(100, 3600)
        relay.RelayHandler.outbox = relay.Outbox(outbox_db)
        relay.RelayHandler.inbox_db = f"{self.tmp.name}/inbox.db"
        relay.RelayHandler.cursor = relay.Cursor(f"{self.tmp.name}/cursor.json")
        relay.RelayHandler.trusted_proxies = loopback
        self.relay, self.relay_url = start(relay.RelayHandler)

    def tearDown(self):
        for s in (self.push, self.inbox, self.relay):
            s.shutdown()
            s.server_close()
        inbox.InboxHandler.store.db.close()
        relay.RelayHandler.outbox.db.close()
        self.tmp.cleanup()

    def reply(self, body, token=REPLY_TOKEN, via="203.0.113.5"):
        return request(self.inbox_url + "/replies", "POST", body,
                       {"Authorization": f"Bearer {token}"}, via=via)

    def fetch(self, path, token=REPLY_TOKEN, via="203.0.113.5"):
        return request(self.inbox_url + path, headers={"Authorization": f"Bearer {token}"}, via=via)

    def notify(self, headline):
        status, body = request(self.relay_url + "/notify", "POST", {"headline": headline}, via=AGENT_IP)
        self.assertEqual(status, 202)
        return body

    # agent -> phone

    def test_json_notify_is_signed_and_pushed(self):
        status, body = request(self.relay_url + "/notify", "POST",
                               {"headline": "Book the 9:40?", "detail": "Holds expire at noon.",
                                "choices": ["Yes", "No"]}, via=AGENT_IP)
        self.assertEqual(status, 202)
        self.assertEqual(len(FakePush.received), 1)

        version, mac, payload = FakePush.received[0].decode().split(".", 2)
        self.assertEqual(version, "v1")
        expected = hmac.new(PUSH_KEY, payload.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(mac, expected)
        message = json.loads(payload)
        self.assertEqual(message["id"], body["id"])
        self.assertEqual(message["choices"], ["Yes", "No"])

    def test_plain_text_notify_splits_headline_from_detail(self):
        status, _ = request(self.relay_url + "/notify", "POST",
                            "Relay test\nIf you can read this, the path works.",
                            {"Content-Type": "text/plain"}, via=AGENT_IP)
        self.assertEqual(status, 202)
        message = json.loads(FakePush.received[0].decode().split(".", 2)[2])
        self.assertEqual(message["headline"], "Relay test")
        self.assertEqual(message["detail"], "If you can read this, the path works.")

    def test_unlisted_device_is_refused(self):
        status, _ = request(self.relay_url + "/notify", "POST", {"headline": "hi"}, via=STRANGER_IP)
        self.assertEqual(status, 403)
        self.assertEqual(FakePush.received, [])

    def test_loopback_without_forwarded_header_is_refused(self):
        status, _ = request(self.relay_url + "/notify", "POST", {"headline": "hi"})
        self.assertEqual(status, 403)

    def test_bad_message_is_rejected(self):
        for body in ({"headline": ""}, {"headline": "x" * 121},
                     {"headline": "a", "choices": ["1", "2", "3", "4", "5"]}):
            status, _ = request(self.relay_url + "/notify", "POST", body, via=AGENT_IP)
            self.assertEqual(status, 400, body)
        self.assertEqual(FakePush.received, [])

    def test_failed_push_still_waits_in_the_outbox(self):
        FakePush.status = 410
        sent = self.notify("hi")
        self.assertFalse(sent["pushed"])
        messages = self.fetch("/messages")[1]["messages"]
        self.assertEqual([json.loads(m["body"].split(".", 2)[2])["id"] for m in messages], [sent["id"]])

    def test_without_a_push_endpoint_messages_wait_to_be_fetched(self):
        relay.RelayHandler.push_endpoint = None
        sent = self.notify("hi")
        self.assertFalse(sent["pushed"])
        self.assertEqual(FakePush.received, [])

        status, body = self.fetch("/messages")
        self.assertEqual(status, 200)
        version, mac, payload = body["messages"][0]["body"].split(".", 2)
        self.assertEqual(mac, hmac.new(PUSH_KEY, payload.encode(), hashlib.sha256).hexdigest())
        self.assertEqual(json.loads(payload)["id"], sent["id"])

    def test_pushed_and_fetched_bodies_are_identical(self):
        self.assertTrue(self.notify("hi")["pushed"])
        self.assertEqual(self.fetch("/messages")[1]["messages"][0]["body"].encode(), FakePush.received[0])

    def test_new_keys_re_sign_the_kept_history(self):
        self.notify("before new keys")
        new_key = b"n" * 43
        self.assertEqual(relay.RelayHandler.outbox.resign(new_key), 1)
        _, mac, payload = self.fetch("/messages")[1]["messages"][0]["body"].split(".", 2)
        self.assertEqual(mac, hmac.new(new_key, payload.encode(), hashlib.sha256).hexdigest())
        self.assertEqual(json.loads(payload)["headline"], "before new keys")
        self.assertEqual(relay.RelayHandler.outbox.resign(new_key), 0)

    def test_a_follow_up_joins_the_first_messages_thread(self):
        first = self.notify("Tailnet test")
        self.assertEqual(first["thread"], first["id"])
        second = request(self.relay_url + "/notify", "POST",
                         {"headline": "Got your Got it", "thread": first["id"]}, via=AGENT_IP)[1]
        # Naming the second message still lands in the first one's thread.
        third = request(self.relay_url + "/notify", "POST",
                        {"headline": "And again", "thread": second["id"]}, via=AGENT_IP)[1]
        self.assertEqual((second["thread"], third["thread"]), (first["id"], first["id"]))
        pushed = [json.loads(b.decode().split(".", 2)[2]) for b in FakePush.received]
        self.assertEqual([m.get("thread") for m in pushed], [None, first["id"], first["id"]])

    def test_unknown_thread_starts_its_own(self):
        status, body = request(self.relay_url + "/notify", "POST",
                               {"headline": "hi", "thread": "m_00000000000000ff"}, via=AGENT_IP)
        self.assertEqual((status, body["thread"]), (202, "m_00000000000000ff"))
        self.assertEqual(request(self.relay_url + "/notify", "POST",
                                 {"headline": "hi", "thread": "not-an-id"}, via=AGENT_IP)[0], 400)

    def test_unpaired_relay_answers_503(self):
        relay.RelayHandler.push_key = None
        status, _ = request(self.relay_url + "/notify", "POST", {"headline": "hi"}, via=AGENT_IP)
        self.assertEqual(status, 503)

    def test_fetch_pages_past_the_tools_place(self):
        ids = [self.notify(f"m{i}")["id"] for i in range(3)]
        first = self.fetch("/messages?limit=2")[1]["messages"]
        self.assertEqual(len(first), 2)
        rest = self.fetch(f"/messages?after={first[-1]['seq']}")[1]["messages"]
        got = [json.loads(m["body"].split(".", 2)[2])["id"] for m in first + rest]
        self.assertEqual(got, ids)
        # Fetching keeps no state: the same read gives the same answer.
        self.assertEqual(self.fetch("/messages?limit=2")[1]["messages"], first)

    def test_fetch_needs_the_token(self):
        self.notify("hi")
        for path in ("/messages", "/replies"):
            self.assertEqual(request(self.inbox_url + path)[0], 401, path)
            self.assertEqual(self.fetch(path, token="nope")[0], 401, path)

    def test_bad_paging_is_rejected(self):
        self.assertEqual(self.fetch("/messages?after=x")[0], 400)

    def test_fetch_before_any_message_is_empty(self):
        self.assertEqual(self.fetch("/messages"), (200, {"messages": []}))

    # phone -> agent

    def test_reply_reaches_agent_once_acknowledged(self):
        self.assertEqual(self.reply({"id": "r1", "messageId": "m_1", "choice": "Yes"})[0], 202)
        self.assertEqual(self.reply({"id": "r2", "text": "also the return"})[0], 202)

        status, body = request(self.relay_url + "/replies", via=AGENT_IP)
        self.assertEqual(status, 200)
        self.assertEqual([r["id"] for r in body["replies"]], ["r1", "r2"])
        self.assertEqual(body["replies"][0]["choice"], "Yes")

        last = body["replies"][-1]["seq"]
        self.assertEqual(request(self.relay_url + "/replies/ack", "POST", {"upTo": last}, via=AGENT_IP)[0], 200)
        self.assertEqual(request(self.relay_url + "/replies", via=AGENT_IP)[1]["replies"], [])

    def test_conversation_started_on_the_phone_reaches_the_agent_and_back(self):
        start = {"id": "r_start", "thread": "t_0123456789abcdef", "title": "Plan Saturday",
                 "text": "Plan Saturday\nHike or the market?"}
        self.assertEqual(self.reply(start)[0], 202)
        got = request(self.relay_url + "/replies", via=AGENT_IP)[1]["replies"][0]
        self.assertEqual((got["thread"], got["title"], got["messageId"]),
                         ("t_0123456789abcdef", "Plan Saturday", None))
        answer = request(self.relay_url + "/notify", "POST",
                         {"headline": "Hike", "thread": got["thread"]}, via=AGENT_IP)[1]
        self.assertEqual(answer["thread"], "t_0123456789abcdef")
        # The phone can read its own start back to rebuild the conversation.
        self.assertEqual(self.fetch("/replies")[1]["replies"][0]["title"], "Plan Saturday")

    def test_bad_thread_or_title_is_rejected(self):
        for body in ({"id": "r1", "text": "x", "thread": "m_0123456789abcdef"},
                     {"id": "r1", "text": "x", "title": "no thread"},
                     {"id": "r1", "text": "x", "thread": "t_0123456789abcdef", "title": "x" * 121}):
            self.assertEqual(self.reply(body)[0], 400, body)

    def test_older_reply_database_gains_the_new_columns(self):
        path = f"{self.tmp.name}/old.db"
        import sqlite3
        old = sqlite3.connect(path)
        old.execute("CREATE TABLE replies (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,"
                    " message_id TEXT, choice TEXT, text TEXT, sent_at TEXT, received_at TEXT NOT NULL)")
        old.execute("INSERT INTO replies (id, choice, received_at) VALUES ('r_old', 'Yes', '2099-01-01T00:00:00Z')")
        old.commit()
        old.close()
        store = inbox.ReplyStore(path)
        try:
            self.assertEqual(store.since(0, 10)[0]["thread"], None)
        finally:
            store.db.close()

    def test_retried_reply_is_stored_once(self):
        self.reply({"id": "r1", "text": "hello"})
        self.assertEqual(self.reply({"id": "r1", "text": "hello"})[0], 202)
        self.assertEqual(len(request(self.relay_url + "/replies", via=AGENT_IP)[1]["replies"]), 1)

    def test_tool_can_read_back_its_replies(self):
        self.reply({"id": "r1", "messageId": "m_1", "choice": "Yes"})
        self.reply({"id": "r2", "messageId": "m_1", "text": "and the return"})
        replies = self.fetch("/replies")[1]["replies"]
        self.assertEqual([(r["id"], r["messageId"]) for r in replies], [("r1", "m_1"), ("r2", "m_1")])
        self.assertEqual(self.fetch(f"/replies?after={replies[0]['seq']}")[1]["replies"][0]["id"], "r2")
        # The tool's reads don't move the agent's cursor.
        self.assertEqual(len(request(self.relay_url + "/replies", via=AGENT_IP)[1]["replies"]), 2)

    def test_stranger_cannot_read_replies(self):
        self.reply({"id": "r1", "text": "hello"})
        self.assertEqual(request(self.relay_url + "/replies", via=STRANGER_IP)[0], 403)

    def test_inbox_rejects_missing_or_wrong_token(self):
        self.assertEqual(request(self.inbox_url + "/replies", "POST", {"id": "r1", "text": "x"})[0], 401)
        self.assertEqual(self.reply({"id": "r1", "text": "x"}, token="nope")[0], 401)

    def test_inbox_offers_nothing_else(self):
        self.reply({"id": "r1", "text": "hello"})
        for method, path in (("GET", "/"), ("DELETE", "/replies"), ("POST", "/notify"),
                             ("POST", "/messages"), ("GET", "/healthz")):
            status, _ = request(self.inbox_url + path, method,
                                headers={"Authorization": f"Bearer {REPLY_TOKEN}"})
            self.assertEqual(status, 404, (method, path))

    def test_inbox_rate_limits_per_sender(self):
        statuses = [self.reply({"id": f"r{i}", "text": "x"}, token="nope", via="198.51.100.1")[0]
                    for i in range(7)]
        self.assertEqual(statuses[-1], 429)
        # A different sender is unaffected.
        self.assertEqual(self.reply({"id": "ok", "text": "x"}, via="198.51.100.2")[0], 202)

    def test_inbox_caps_body_size(self):
        status, _ = self.reply({"id": "r1", "text": "x" * 5000})
        self.assertEqual(status, 413)

    def test_empty_reply_is_rejected(self):
        self.assertEqual(self.reply({"id": "r1", "text": "  "})[0], 400)


if __name__ == "__main__":
    unittest.main()
