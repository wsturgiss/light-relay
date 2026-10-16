"""The reply webhook: one POST per new reply, retries, and re-sends while unacknowledged."""

import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from lightrelay import webhook


class Hook(BaseHTTPRequestHandler):
    statuses = []
    received = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Hook.received.append((self.headers["Authorization"], body))
        status = Hook.statuses.pop(0) if Hook.statuses else 200
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


class Webhook(unittest.TestCase):
    def setUp(self):
        Hook.statuses, Hook.received = [], []
        self.server = HTTPServer(("127.0.0.1", 0), Hook)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/hook"
        self.seq = 7

    def tearDown(self):
        self.server.shutdown()

    def notifier(self, **kw):
        return webhook.Notifier(self.url, "k3y", lambda: self.seq, backoff=(0.05, 0.1), check_every=0.05, **kw)

    def wait_for(self, n):
        deadline = time.time() + 5
        while len(Hook.received) < n and time.time() < deadline:
            time.sleep(0.02)

    def test_a_new_reply_posts_newest_seq_once(self):
        self.notifier().start().poke()
        self.wait_for(1)
        time.sleep(0.3)
        self.assertEqual(Hook.received, [("Bearer k3y", {"newest_seq": 7})])

    def test_failures_are_retried(self):
        Hook.statuses = [500, 503]
        self.notifier().start().poke()
        self.wait_for(3)
        self.assertEqual(len(Hook.received), 3)

    def test_resent_while_the_agent_is_behind(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "cursor.json")
            with open(path, "w") as f:
                json.dump({"seq": 0, "agents": {"100.1.1.1": 5}}, f)
            n = self.notifier(cursor_path=path, agent="100.1.1.1", reping=0.2).start()
            n.poke()
            self.wait_for(2)
            self.assertGreaterEqual(len(Hook.received), 2)
            with open(path, "w") as f:
                json.dump({"seq": 0, "agents": {"100.1.1.1": 7}}, f)
            time.sleep(0.3)
            count = len(Hook.received)
            time.sleep(0.5)
            self.assertEqual(len(Hook.received), count)


if __name__ == "__main__":
    unittest.main()
