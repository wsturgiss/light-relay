"""Short-lived session tokens: issuing, checking, and renewal over TLS on loopback."""

import json
import secrets
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from lightrelay import inbox, session
from lightrelay.common import RateLimiter, serve

SECRET = b"t" * 43


class Tokens(unittest.TestCase):
    def test_issued_token_is_valid_until_it_expires(self):
        token, expires = session.issue(SECRET, 3600, now=1000)
        self.assertEqual(expires, 4600)
        self.assertTrue(session.valid(token, SECRET, 3600, now=1001))
        self.assertFalse(session.valid(token, SECRET, 3600, now=4600))

    def test_other_keys_and_tampering_fail(self):
        token, _ = session.issue(SECRET, 3600, now=1000)
        self.assertFalse(session.valid(token, b"n" * 43, 3600, now=1001))
        prefix, expires, mac = token.split(".")
        self.assertFalse(session.valid(f"{prefix}.{int(expires) + 99999}.{mac}", SECRET, 10**9, now=1001))
        self.assertFalse(session.valid("s1.notanumber." + mac, SECRET, 3600, now=1001))

    def test_a_token_longer_than_a_session_is_refused(self):
        # Even correctly signed: a session can't outlive the configured lifetime.
        token, _ = session.issue(SECRET, 30 * 86400, now=1000)
        self.assertFalse(session.valid(token, SECRET, 72 * 3600, now=1001))

    def test_nonces_are_single_use(self):
        nonces = session.Nonces()
        self.assertTrue(nonces.fresh("ab" * 16, now=1000))
        self.assertFalse(nonces.fresh("ab" * 16, now=1001))
        # Long after the clock window it's forgotten, and stale anyway.
        self.assertTrue(nonces.fresh("ab" * 16, now=1000 + 3 * session.CLOCK_SKEW))


def request(url, method="GET", body=None, token=None, context=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5, context=context) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as e:
        with e:
            return e.code, json.loads(e.read() or b"{}")


@unittest.skipUnless(shutil.which("openssl"), "needs openssl to make a test certificate")
class Renewal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cert, key = f"{self.tmp.name}/cert.pem", f"{self.tmp.name}/key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
                        "-nodes", "-keyout", key, "-out", cert, "-days", "1", "-subj", "/CN=test",
                        "-addext", "subjectAltName=IP:127.0.0.1"], check=True, capture_output=True)
        # What the phone does: trust this one certificate and nothing else.
        self.pinned = ssl.create_default_context(cafile=cert)

        inbox.SessionHandler.token = SECRET
        inbox.SessionHandler.session_ttl = 72 * 3600
        inbox.SessionHandler.limit = RateLimiter(100, 60)
        inbox.SessionHandler.nonces = session.Nonces()
        self.lan = inbox.serve_sessions("127.0.0.1", 0, cert, key)
        threading.Thread(target=self.lan.serve_forever, daemon=True).start()
        self.lan_url = f"https://127.0.0.1:{self.lan.server_address[1]}"

        inbox.InboxHandler.store = inbox.ReplyStore(f"{self.tmp.name}/inbox.db")
        inbox.InboxHandler.outbox_db = f"{self.tmp.name}/outbox.db"
        inbox.InboxHandler.token = SECRET
        inbox.InboxHandler.accept_reply_token = False
        inbox.InboxHandler.session_ttl = 72 * 3600
        inbox.InboxHandler.per_ip = RateLimiter(100, 60)
        inbox.InboxHandler.overall = RateLimiter(1000, 60)
        self.public = serve(inbox.InboxHandler, "127.0.0.1", 0)
        threading.Thread(target=self.public.serve_forever, daemon=True).start()
        self.public_url = f"http://127.0.0.1:{self.public.server_address[1]}"

    def tearDown(self):
        for s in (self.lan, self.public):
            s.shutdown()
            s.server_close()
        inbox.InboxHandler.store.db.close()
        inbox.InboxHandler.accept_reply_token = True
        self.tmp.cleanup()

    def renew(self, secret=SECRET, at=None, nonce=None):
        at = int(time.time()) if at is None else at
        nonce = nonce or secrets.token_hex(16)
        return request(self.lan_url + "/session", "POST",
                       {"at": at, "nonce": nonce, "proof": session.proof(secret, at, nonce)}, context=self.pinned)

    def test_renewed_session_opens_the_public_inbox(self):
        status, body = self.renew()
        self.assertEqual(status, 200)
        self.assertTrue(body["session"].startswith("s1."))
        self.assertEqual(request(self.public_url + "/messages", token=body["session"])[0], 200)
        self.assertEqual(request(self.public_url + "/replies", "POST", {"id": "r1", "text": "hi"},
                                 token=body["session"])[0], 202)

    def test_the_reply_token_no_longer_opens_the_public_inbox(self):
        self.assertEqual(request(self.public_url + "/messages", token=SECRET.decode())[0], 401)
        # Until it's told to, while the phone moves over.
        inbox.InboxHandler.accept_reply_token = True
        self.assertEqual(request(self.public_url + "/messages", token=SECRET.decode())[0], 200)

    def test_new_keys_end_every_session(self):
        token = self.renew()[1]["session"]
        inbox.InboxHandler.token = b"n" * 43
        self.assertEqual(request(self.public_url + "/messages", token=token)[0], 401)

    def test_renewal_needs_the_reply_token(self):
        self.assertEqual(self.renew(secret=b"x" * 43)[0], 401)

    def test_a_renewal_cant_be_replayed_or_backdated(self):
        nonce = secrets.token_hex(16)
        self.assertEqual(self.renew(nonce=nonce)[0], 200)
        self.assertEqual(self.renew(nonce=nonce)[0], 401)
        self.assertEqual(self.renew(at=int(time.time()) - 3600)[0], 400)

    def test_only_https_with_the_pinned_certificate(self):
        # Another certificate: the phone's pin refuses the connection outright.
        other = ssl.create_default_context()
        with self.assertRaises(urllib.error.URLError):
            request(self.lan_url + "/session", "POST", {}, context=other)
        # Plain HTTP to the TLS port gets nowhere.
        with self.assertRaises((urllib.error.URLError, ConnectionError, OSError)):
            request(self.lan_url.replace("https:", "http:") + "/session", "POST", {})

    def test_session_door_offers_nothing_else(self):
        for method, path in (("GET", "/session"), ("GET", "/messages"), ("POST", "/replies")):
            self.assertEqual(request(self.lan_url + path, method, {} if method == "POST" else None,
                                     context=self.pinned)[0], 404, (method, path))


if __name__ == "__main__":
    unittest.main()
