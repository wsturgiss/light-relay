"""Short-lived session tokens for the public inbox.

The reply token from pairing is the long-lived secret. With sessions on, it no
longer opens the public inbox. The phone trades it for a session token on the
LAN (POST /session, HTTPS with a certificate the phone pins), and presents that
token over Funnel until it expires. A lost phone stops working by itself
within the session's lifetime, a leaked session as soon as the phone renews,
and new keys on the phone end every session at once.

A session token is `s1.<expiry, unix seconds>.<hex HMAC-SHA256(reply token,
"session:<expiry>")>`. The inbox checks it with the reply token and one stored
number: the expiry of the newest session issued. Each renewal retires every
earlier session (after a short grace, so a request already in flight with the
old one still lands), so a leaked session lasts only until the phone next
renews at home.

Renewal proves the phone holds the reply token without sending it: the phone
signs `renew:<unix seconds>:<nonce>` with it. The request carries the time, the
nonce and the signature.
"""

import hashlib
import hmac
import json
import os
import re
import threading
import time

PREFIX = "s1"
TOKEN_RE = re.compile(r"^s1\.(\d{1,12})\.([0-9a-f]{64})$")
NONCE_RE = re.compile(r"^[0-9a-f]{32,64}$")
# How far the phone's clock may be from ours when it asks for a session.
CLOCK_SKEW = 300
# How long a session outlives the next one being issued.
RETIRE_GRACE = 600


def _mac(secret, message):
    return hmac.new(secret, message.encode(), hashlib.sha256).hexdigest()


def issue(secret, ttl, now=None):
    """A session token good for `ttl` seconds, and when it expires."""
    expires = int(now if now is not None else time.time()) + ttl
    return f"{PREFIX}.{expires}.{_mac(secret, f'session:{expires}')}", expires


def valid(token, secret, max_ttl, now=None, newest=0, grace=RETIRE_GRACE):
    """Signed with `secret`, unexpired, not good for longer than a session can be, and
    not retired: issued no more than `grace` seconds before the newest session, whose
    expiry is `newest`. Sessions all last the same, so expiry orders them by issue."""
    match = TOKEN_RE.match(token)
    if not match:
        return False
    expires = int(match.group(1))
    now = now if now is not None else time.time()
    if not now < expires <= now + max_ttl:
        return False
    if expires < newest - grace:
        return False
    return hmac.compare_digest(match.group(2), _mac(secret, f"session:{expires}"))


def proof(secret, at, nonce):
    """What the phone sends to renew: the reply token's signature over the time and nonce."""
    return _mac(secret, f"renew:{at}:{nonce}")


class Newest:
    """The expiry of the newest session issued, kept on disk so a restart doesn't bring
    retired sessions back. Set, not raised: after a shorter SESSION_TTL_HOURS, the
    newest session's expiry is earlier than the last one's."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        try:
            with open(path) as f:
                self.value = int(json.load(f)["expires"])
        except (FileNotFoundError, ValueError, KeyError):
            self.value = 0

    def set(self, expires):
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"expires": expires}, f)
            os.replace(tmp, self.path)
            self.value = expires


class Nonces:
    """Nonces seen within the clock window, so a captured renewal can't be replayed."""

    def __init__(self):
        self.seen = {}
        self.lock = threading.Lock()

    def fresh(self, nonce, now=None):
        now = now if now is not None else time.time()
        with self.lock:
            self.seen = {n: t for n, t in self.seen.items() if t > now - 2 * CLOCK_SKEW}
            if nonce in self.seen:
                return False
            self.seen[nonce] = now
            return True
