"""Short-lived session tokens for the public inbox.

The reply token from pairing is the long-lived secret. With sessions on, it no
longer opens the public inbox. The phone trades it for a session token on the
LAN (POST /session, HTTPS with a certificate the phone pins), and presents that
token over Funnel until it expires. A lost phone, or a leaked session token,
stops working by itself within the session's lifetime, and new keys on the
phone end every session at once.

A session token is `s1.<expiry, unix seconds>.<hex HMAC-SHA256(reply token,
"session:<expiry>")>`. The inbox checks it with the reply token alone: nothing
is stored.

Renewal proves the phone holds the reply token without sending it: the phone
signs `renew:<unix seconds>:<nonce>` with it. The request carries the time, the
nonce and the signature.
"""

import hashlib
import hmac
import re
import threading
import time

PREFIX = "s1"
TOKEN_RE = re.compile(r"^s1\.(\d{1,12})\.([0-9a-f]{64})$")
NONCE_RE = re.compile(r"^[0-9a-f]{32,64}$")
# How far the phone's clock may be from ours when it asks for a session.
CLOCK_SKEW = 300


def _mac(secret, message):
    return hmac.new(secret, message.encode(), hashlib.sha256).hexdigest()


def issue(secret, ttl, now=None):
    """A session token good for `ttl` seconds, and when it expires."""
    expires = int(now if now is not None else time.time()) + ttl
    return f"{PREFIX}.{expires}.{_mac(secret, f'session:{expires}')}", expires


def valid(token, secret, max_ttl, now=None):
    """Signed with `secret`, unexpired, and not good for longer than a session can be."""
    match = TOKEN_RE.match(token)
    if not match:
        return False
    expires = int(match.group(1))
    now = now if now is not None else time.time()
    if not now < expires <= now + max_ttl:
        return False
    return hmac.compare_digest(match.group(2), _mac(secret, f"session:{expires}"))


def proof(secret, at, nonce):
    """What the phone sends to renew: the reply token's signature over the time and nonce."""
    return _mac(secret, f"renew:{at}:{nonce}")


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
