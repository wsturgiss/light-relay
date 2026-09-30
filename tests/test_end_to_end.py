"""The whole loop on loopback: agent -> relay -> fake push endpoint, and
phone -> inbox -> relay -> agent. Run with `python -m unittest`."""

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

        inbox.InboxHandler.store = inbox.ReplyStore(f"{self.tmp.name}/inbox.db")
        inbox.InboxHandler.token = REPLY_TOKEN.encode()
        inbox.InboxHandler.per_ip = RateLimiter(5, 60)
        inbox.InboxHandler.overall = RateLimiter(1000, 60)
        inbox.InboxHandler.trusted_proxies = loopback
        self.inbox, self.inbox_url = start(inbox.InboxHandler)

        relay.RelayHandler.allowed_peers = frozenset({ipaddress.ip_address(AGENT_IP)})
        relay.RelayHandler.push_endpoint = push_url + "/push/abc"
        relay.RelayHandler.push_key = PUSH_KEY
        relay.RelayHandler.notify_limit = RateLimiter(100, 3600)
        relay.RelayHandler.inbox_db = f"{self.tmp.name}/inbox.db"
        relay.RelayHandler.cursor = relay.Cursor(f"{self.tmp.name}/cursor.json")
        relay.RelayHandler.trusted_proxies = loopback
        self.relay, self.relay_url = start(relay.RelayHandler)

    def tearDown(self):
        for s in (self.push, self.inbox, self.relay):
            s.shutdown()
            s.server_close()
        self.tmp.cleanup()

    def reply(self, body, token=REPLY_TOKEN, via="203.0.113.5"):
        return request(self.inbox_url + "/replies", "POST", body,
                       {"Authorization": f"Bearer {token}"}, via=via)

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

    def test_push_failure_is_reported_to_agent(self):
        FakePush.status = 410
        status, body = request(self.relay_url + "/notify", "POST", {"headline": "hi"}, via=AGENT_IP)
        self.assertEqual(status, 502)
        self.assertIn("410", body["error"])

    def test_unpaired_relay_answers_503(self):
        relay.RelayHandler.push_endpoint = None
        status, _ = request(self.relay_url + "/notify", "POST", {"headline": "hi"}, via=AGENT_IP)
        self.assertEqual(status, 503)

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

    def test_retried_reply_is_stored_once(self):
        self.reply({"id": "r1", "text": "hello"})
        self.assertEqual(self.reply({"id": "r1", "text": "hello"})[0], 202)
        self.assertEqual(len(request(self.relay_url + "/replies", via=AGENT_IP)[1]["replies"]), 1)

    def test_stranger_cannot_read_replies(self):
        self.reply({"id": "r1", "text": "hello"})
        self.assertEqual(request(self.relay_url + "/replies", via=STRANGER_IP)[0], 403)

    def test_inbox_rejects_missing_or_wrong_token(self):
        self.assertEqual(request(self.inbox_url + "/replies", "POST", {"id": "r1", "text": "x"})[0], 401)
        self.assertEqual(self.reply({"id": "r1", "text": "x"}, token="nope")[0], 401)

    def test_inbox_offers_nothing_but_posting_replies(self):
        self.reply({"id": "r1", "text": "hello"})
        for method, path in (("GET", "/replies"), ("GET", "/"), ("DELETE", "/replies"), ("POST", "/notify")):
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
