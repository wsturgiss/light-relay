"""Wake an agent when a reply arrives: POST {"newest_seq": n} to its webhook.

Runs on its own thread so storing a reply never waits on the network. A failed
POST is retried with exponential backoff. If the agent's acknowledged position
(the relay's cursor.json, per caller IP) stays behind the newest reply, the
webhook is sent again at most every `reping` seconds.
"""

import json
import threading
import time
import urllib.error
import urllib.request

from .common import log


def read_ack(path, agent):
    """The agent's acknowledged seq from the relay's cursor file, or None."""
    try:
        with open(path) as f:
            data = json.load(f)
        return int(data.get("agents", {}).get(agent, data.get("seq", 0)))
    except (OSError, ValueError, TypeError, AttributeError):
        return None


class Notifier:
    def __init__(self, url, key, newest, cursor_path=None, agent=None, reping=900,
                 backoff=(5, 600), timeout=10, check_every=60, clock=time.monotonic):
        self.url, self.key, self.newest = url, key, newest
        self.cursor_path, self.agent, self.reping = cursor_path, agent, reping
        self.backoff_min, self.backoff_max = backoff
        self.timeout, self.check_every, self.clock = timeout, check_every, clock
        self.wake = threading.Event()
        self.pending = False      # a new reply hasn't been announced yet
        self.sent_seq = 0         # newest seq announced successfully
        self.sent_at = None       # when (clock)
        self.lock = threading.Lock()

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="webhook").start()
        return self

    def poke(self):
        """A reply was stored. Returns at once."""
        with self.lock:
            self.pending = True
        self.wake.set()

    def due(self):
        """The seq to announce now, or None."""
        newest = self.newest()
        if not newest:
            return None
        with self.lock:
            if self.pending:
                return newest
        if not self.cursor_path or not self.agent or self.sent_at is None:
            return None
        ack = read_ack(self.cursor_path, self.agent)
        if ack is not None and newest > ack and self.clock() - self.sent_at >= self.reping:
            return newest
        return None

    def post(self, seq):
        req = urllib.request.Request(self.url, data=json.dumps({"newest_seq": seq}).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {self.key}",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return resp.status

    def send(self, seq):
        """POST until it succeeds, backing off exponentially. A newer reply that
        arrives meanwhile is folded into the next attempt."""
        delay = self.backoff_min
        while True:
            with self.lock:
                self.pending = False
            try:
                status = self.post(seq)
                log.info("webhook: newest_seq=%d -> HTTP %d", seq, status)
                self.sent_seq, self.sent_at = seq, self.clock()
                return status
            except urllib.error.HTTPError as e:
                e.close()
                log.warning("webhook: newest_seq=%d -> HTTP %d; retrying in %gs", seq, e.code, delay)
            except Exception as e:  # network, timeout, DNS
                log.warning("webhook: newest_seq=%d failed (%s); retrying in %gs", seq, e, delay)
            with self.lock:
                self.pending = True
            self.wake.wait(delay)
            self.wake.clear()
            seq = max(seq, self.newest() or 0)
            delay = min(delay * 2, self.backoff_max)

    def _run(self):
        while True:
            self.wake.wait(self.check_every)
            self.wake.clear()
            try:
                seq = self.due()
                if seq is not None:
                    self.send(seq)
            except Exception:
                log.exception("webhook: unexpected error")
