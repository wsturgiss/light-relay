"""Pieces shared by the relay and the reply inbox. Standard library only."""

import ipaddress
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("lightrelay")

# A reply as both the agent (via the relay) and the tool (via the inbox) read it.
REPLY_COLUMNS = "seq, id, message_id, thread, title, choice, text, sent_at, received_at"


def reply_from_row(r):
    return {"seq": r[0], "id": r[1], "messageId": r[2], "thread": r[3], "title": r[4],
            "choice": r[5], "text": r[6], "sentAt": r[7], "receivedAt": r[8]}


def env(name, default=None, required=False):
    value = os.environ.get(name, "").strip()
    if not value:
        if required:
            raise SystemExit(f"{name} is not set")
        return default
    return value


def env_int(name, default):
    return int(env(name, str(default)))


def parse_ips(value):
    """Comma-separated IPs, e.g. "100.64.0.7, fd7a:115c:a1e0::7"."""
    if not value:
        return frozenset()
    return frozenset(ipaddress.ip_address(p.strip()) for p in value.split(",") if p.strip())


def setup_logging():
    logging.basicConfig(
        level=env("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )


class RateLimiter:
    """Token bucket per key. `rate` tokens per `per` seconds, bursting to `rate`."""

    def __init__(self, rate, per):
        self.capacity = float(rate)
        self.refill = rate / per
        self.buckets = {}
        self.lock = threading.Lock()

    def allow(self, key):
        now = time.monotonic()
        with self.lock:
            tokens, last = self.buckets.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.refill)
            allowed = tokens >= 1
            if allowed:
                tokens -= 1
            self.buckets[key] = (tokens, now)
            if len(self.buckets) > 10_000:
                self._prune(now)
            return allowed

    def _prune(self, now):
        full = [k for k, (t, last) in self.buckets.items()
                if t + (now - last) * self.refill >= self.capacity]
        for k in full:
            del self.buckets[k]


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class Handler(BaseHTTPRequestHandler):
    """Base handler: JSON replies, bounded body reads, and the real client IP.

    Tailscale Serve and Funnel both reach us as a reverse proxy on loopback and
    put the original sender in a forwarded header. That header is only believed
    when the socket peer is one of `trusted_proxies`; anyone else is taken at
    their socket address.
    """

    server_version = "light-relay"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 15  # seconds per socket read; stops slow-drip clients holding threads

    trusted_proxies = frozenset()
    client_ip_header = "X-Forwarded-For"
    max_body = 4096

    def client_ip(self):
        peer = ipaddress.ip_address(self.client_address[0])
        if peer in self.trusted_proxies:
            forwarded = self.headers.get(self.client_ip_header, "")
            # The nearest proxy appends last, so the last entry is the one it saw.
            last = forwarded.split(",")[-1].strip()
            if last:
                try:
                    return ipaddress.ip_address(last)
                except ValueError:
                    pass
        return peer

    def read_body(self):
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise HttpError(411, "Content-Length required")
        if length < 0 or length > self.max_body:
            raise HttpError(413, f"body over {self.max_body} bytes")
        return self.rfile.read(length)

    def read_json(self):
        try:
            data = json.loads(self.read_body())
        except (ValueError, UnicodeDecodeError):
            raise HttpError(400, "body is not JSON")
        if not isinstance(data, dict):
            raise HttpError(400, "body must be a JSON object")
        return data

    def send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status, message):
        # Don't keep a connection open for a client we just turned away.
        self.close_connection = True
        self.send_json(status, {"error": message})

    def dispatch(self, method):
        try:
            self.route(method)
        except HttpError as e:
            self.send_error_json(e.status, e.message)
        except Exception:
            log.exception("unhandled error on %s %s", method, self.path)
            self.send_error_json(500, "internal error")

    def route(self, method):
        raise HttpError(404, "not found")

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def do_HEAD(self):
        self.dispatch("HEAD")

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.client_address[0], fmt % args)


def serve(handler_cls, host, port):
    server = ThreadingHTTPServer((host, port), handler_cls)
    server.daemon_threads = True
    log.info("%s listening on %s:%d", handler_cls.__name__, host, port)
    return server
