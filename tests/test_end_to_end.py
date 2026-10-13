"""The whole loop on loopback: agent -> relay -> fake push endpoint,
agent -> relay -> outbox -> inbox -> phone, and phone -> inbox -> relay -> agent. Run with `python -m unittest`."""

import hashlib
import hmac
import ipaddress
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

from lightrelay import inbox, relay, transcribe
from lightrelay.common import RateLimiter, serve

AGENT_IP = "100.64.0.7"
STRANGER_IP = "100.64.0.99"
PUSH_KEY = b"k" * 43
REPLY_TOKEN = "t" * 43
VOICE = b"OggS\x00\x02" + b"\x00" * 200
PHOTO = b"\xff\xd8\xff\xe0" + b"\x01" * 300


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


class FakeTranscriber(BaseHTTPRequestHandler):
    """An OpenAI-style /audio/transcriptions endpoint."""
    received = []
    status = 200
    text = "Pick up milk on the way home."
    error = {"error": {"code": "invalid_api_key"}}

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.received.append((self.headers["Authorization"], body))
        payload = json.dumps({"text": self.text} if self.status == 200 else self.error).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

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
        relay.RelayHandler.attachments_dir = inbox.InboxHandler.store.attachments_dir
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

    def upload(self, attachment_id, data, content_type="audio/ogg", token=REPLY_TOKEN):
        return request(self.inbox_url + f"/attachments/{attachment_id}", "PUT", data,
                       {"Authorization": f"Bearer {token}", "Content-Type": content_type},
                       via="203.0.113.5")

    def download(self, attachment_id, via=AGENT_IP):
        req = urllib.request.Request(self.relay_url + f"/attachments/{attachment_id}",
                                     headers={"X-Forwarded-For": via})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.headers.get_content_type(), resp.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, None, e.read()

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
            self.assertEqual((store.since(0, 10)[0]["thread"], store.since(0, 10)[0]["attachments"]),
                             (None, []))
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
        inbox.InboxHandler.per_ip = RateLimiter(100, 60)
        self.reply({"id": "r1", "text": "hello"})
        for method, path in (("GET", "/"), ("DELETE", "/replies"), ("POST", "/notify"),
                             ("POST", "/messages"), ("GET", "/healthz"),
                             ("GET", "/attachments/a_0000000000000001")):
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

    # attachments: voice notes and photos, phone -> agent

    def test_voice_note_reaches_the_agent(self):
        self.assertEqual(self.upload("a_0000000000000001", VOICE)[0], 201)
        self.assertEqual(self.reply({"id": "r1", "messageId": "m_1",
                                     "attachments": ["a_0000000000000001"]})[0], 202)
        got = request(self.relay_url + "/replies", via=AGENT_IP)[1]["replies"][0]
        self.assertEqual((got["text"], got["choice"]), (None, None))
        self.assertEqual(got["attachments"],
                         [{"id": "a_0000000000000001", "type": "audio/ogg", "bytes": len(VOICE)}])
        self.assertEqual(self.download("a_0000000000000001"), (200, "audio/ogg", VOICE))
        # The tool sees the same thing when it rebuilds its history.
        self.assertEqual(self.fetch("/replies")[1]["replies"][0]["attachments"], got["attachments"])

    def test_photo_with_text(self):
        inbox.InboxHandler.per_ip = RateLimiter(100, 60)
        self.upload("a_00000000000000a1", PHOTO, "image/jpeg")
        self.upload("a_00000000000000a2", b"\x89PNG\r\n\x1a\n" + b"\x00" * 50, "image/png")
        self.reply({"id": "r1", "text": "Which of these?",
                    "attachments": ["a_00000000000000a1", "a_00000000000000a2"]})
        got = request(self.relay_url + "/replies", via=AGENT_IP)[1]["replies"][0]
        self.assertEqual([a["type"] for a in got["attachments"]], ["image/jpeg", "image/png"])
        self.assertEqual(self.download("a_00000000000000a1")[2], PHOTO)

    def test_upload_must_be_what_it_says(self):
        inbox.InboxHandler.per_ip = RateLimiter(100, 60)
        self.assertEqual(self.upload("a_0000000000000001", PHOTO, "audio/ogg")[0], 400)
        self.assertEqual(self.upload("a_0000000000000001", b"", "audio/ogg")[0], 400)
        self.assertEqual(self.upload("a_0000000000000001", VOICE, "application/pdf")[0], 415)
        self.assertEqual(self.upload("a_not_hex", VOICE)[0], 400)
        self.assertEqual(self.upload("../../etc/passwd", VOICE)[0], 404)
        self.assertEqual(self.upload("a_0000000000000001", VOICE, token="nope")[0], 401)
        # Nothing was kept from any of those.
        self.assertEqual(self.reply({"id": "r1", "attachments": ["a_0000000000000001"]})[0], 400)
        self.assertEqual(os.listdir(inbox.InboxHandler.store.attachments_dir), [])

    def test_upload_retries_are_stored_once(self):
        self.assertEqual(self.upload("a_0000000000000001", VOICE)[0], 201)
        self.assertEqual(self.upload("a_0000000000000001", VOICE)[0], 200)
        self.assertEqual(self.upload("a_0000000000000001", VOICE + b"x")[0], 409)
        self.assertEqual(self.download("a_0000000000000001")[2], VOICE)

    def test_reply_needs_its_attachments_uploaded_first(self):
        self.assertEqual(self.reply({"id": "r1", "attachments": ["a_00000000000000ff"]})[0], 400)
        for bad in (["nope"], ["a_0000000000000001"] * 2, "a_0000000000000001"):
            self.assertEqual(self.reply({"id": "r1", "text": "x", "attachments": bad})[0], 400, bad)

    def test_upload_size_is_capped(self):
        inbox.InboxHandler.max_attachment = 100
        try:
            self.assertEqual(self.upload("a_0000000000000001", VOICE)[0], 413)
        finally:
            inbox.InboxHandler.max_attachment = 10 << 20

    def test_oldest_attachments_go_past_the_total(self):
        inbox.InboxHandler.per_ip = RateLimiter(100, 60)
        inbox.InboxHandler.store.attachment_total = len(VOICE) * 2
        for i in range(3):
            self.upload(f"a_000000000000000{i}", VOICE)
        self.assertEqual(self.download("a_0000000000000000")[0], 404)
        self.assertEqual(self.download("a_0000000000000002")[0], 200)
        self.assertEqual(len(os.listdir(inbox.InboxHandler.store.attachments_dir)), 2)

    def test_expired_attachment_is_gone(self):
        self.upload("a_0000000000000001", VOICE)
        store = inbox.InboxHandler.store
        store.db.execute("UPDATE attachments SET received_at = '2000-01-01T00:00:00Z'")
        store.attachment_days = 30
        self.upload("a_0000000000000002", VOICE)
        self.assertEqual(self.download("a_0000000000000001")[0], 404)

    def test_stranger_cannot_download(self):
        self.upload("a_0000000000000001", VOICE)
        self.assertEqual(self.download("a_0000000000000001", via=STRANGER_IP)[0], 403)
        self.assertEqual(self.download("a_bad")[0], 400)


    # transcripts: voice notes to text, through the transcription service

    def transcribing(self, status=200):
        FakeTranscriber.received, FakeTranscriber.status = [], status
        inbox.InboxHandler.per_ip = RateLimiter(1000, 60)  # wait_for polls
        server, url = start(FakeTranscriber)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        worker = transcribe.Transcriber(inbox.InboxHandler.store, "sk-test", url + "/v1/audio/transcriptions",
                                        retry_delay=0).start()
        inbox.InboxHandler.transcriber = worker
        self.addCleanup(setattr, inbox.InboxHandler, "transcriber", None)
        self.addCleanup(worker.stop)
        return worker

    def wait_for(self, check):
        for _ in range(100):
            if check():
                return
            time.sleep(0.02)
        self.fail("timed out")

    def test_voice_note_is_transcribed_for_the_phone_and_the_agent(self):
        self.transcribing()
        self.upload("a_0000000000000001", VOICE)
        self.reply({"id": "r1", "messageId": "m_1", "attachments": ["a_0000000000000001"]})
        self.wait_for(lambda: self.fetch("/transcripts")[1]["transcripts"])
        self.assertEqual(self.fetch("/transcripts")[1]["transcripts"],
                         [{"seq": 1, "id": "a_0000000000000001", "text": "Pick up milk on the way home."}])
        self.assertEqual(self.fetch("/transcripts?after=1")[1]["transcripts"], [])
        got = request(self.relay_url + "/replies", via=AGENT_IP)[1]["replies"][0]
        self.assertEqual(got["attachments"][0]["transcript"], "Pick up milk on the way home.")
        self.assertEqual(self.fetch("/replies")[1]["replies"][0]["attachments"], got["attachments"])
        # What went to the service: the key, the model and the file, named so it knows the format.
        auth, body = FakeTranscriber.received[0]
        self.assertEqual(auth, "Bearer sk-test")
        self.assertIn(b'name="model"\r\n\r\ngpt-4o-mini-transcribe', body)
        self.assertIn(b'filename="a_0000000000000001.ogg"', body)
        self.assertIn(VOICE, body)

    def test_photos_and_retries_are_not_transcribed(self):
        self.transcribing()
        self.upload("a_00000000000000a1", PHOTO, "image/jpeg")
        self.upload("a_0000000000000001", VOICE)
        self.upload("a_0000000000000001", VOICE)
        self.wait_for(lambda: self.fetch("/transcripts")[1]["transcripts"])
        time.sleep(0.1)
        self.assertEqual(len(FakeTranscriber.received), 1)

    def test_refused_transcription_says_why(self):
        self.transcribing(status=401)
        self.upload("a_0000000000000001", VOICE)
        self.wait_for(lambda: self.fetch("/transcripts")[1]["transcripts"])
        self.assertEqual(len(FakeTranscriber.received), 1)  # not retried
        self.assertEqual(self.fetch("/transcripts")[1]["transcripts"],
                         [{"seq": 1, "id": "a_0000000000000001", "text": "", "error": "Transcription key refused"}])
        self.reply({"id": "r1", "attachments": ["a_0000000000000001"]})
        got = request(self.relay_url + "/replies", via=AGENT_IP)[1]["replies"][0]["attachments"][0]
        self.assertNotIn("transcript", got)
        self.assertEqual(got["transcriptError"], "Transcription key refused")

    def test_out_of_credit_is_not_retried_but_comes_back_after_a_restart(self):
        FakeTranscriber.error = {"error": {"code": "insufficient_quota", "message": "You have no credits remaining."}}
        self.addCleanup(setattr, FakeTranscriber, "error", {"error": {"code": "invalid_api_key"}})
        worker = self.transcribing(status=429)
        self.upload("a_0000000000000001", VOICE)
        self.wait_for(lambda: self.fetch("/transcripts")[1]["transcripts"])
        self.assertEqual(len(FakeTranscriber.received), 1)
        self.assertEqual(self.fetch("/transcripts")[1]["transcripts"][0]["error"], "No transcription credit left")
        # Credit added, inbox restarted: the note is tried again, and the text follows the error.
        worker.stop()
        self.transcribing()
        self.wait_for(lambda: self.fetch("/transcripts?after=1")[1]["transcripts"])
        self.assertEqual(self.fetch("/transcripts?after=1")[1]["transcripts"],
                         [{"seq": 2, "id": "a_0000000000000001", "text": "Pick up milk on the way home."}])

    def test_service_errors_are_retried(self):
        self.transcribing(status=503)
        self.upload("a_0000000000000001", VOICE)
        self.wait_for(lambda: self.fetch("/transcripts")[1]["transcripts"])
        self.assertEqual(len(FakeTranscriber.received), transcribe.ATTEMPTS)
        self.assertEqual(self.fetch("/transcripts")[1]["transcripts"][0]["error"], "Transcription service unavailable")

    def test_notes_missed_while_down_are_transcribed_at_start(self):
        self.upload("a_0000000000000001", VOICE)
        self.transcribing()
        self.wait_for(lambda: self.fetch("/transcripts")[1]["transcripts"])

    def test_relay_reads_an_inbox_without_transcripts(self):
        self.upload("a_0000000000000001", VOICE)
        self.reply({"id": "r1", "attachments": ["a_0000000000000001"]})
        inbox.InboxHandler.store.db.execute("DROP TABLE transcripts")
        got = request(self.relay_url + "/replies", via=AGENT_IP)[1]["replies"]
        self.assertEqual(len(got), 1)


if __name__ == "__main__":
    unittest.main()
