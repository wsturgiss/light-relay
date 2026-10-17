"""Voice notes to text, through a cloud speech-to-text API.

The inbox hands each voice note to a `Transcriber` as it's stored. A worker
thread sends it to an OpenAI-compatible `/audio/transcriptions` endpoint
(OpenAI by default; Groq speaks the same API, and xAI's /v1/stt takes the
same upload) and keeps the text in
the inbox database, where the phone and the agent pick it up. Without a key
nothing is sent anywhere, and voice notes go through as before.
"""

import json
import os
import queue
import secrets
import threading
import time
import urllib.error
import urllib.request

from .common import log

DEFAULT_URL = "https://api.openai.com/v1/audio/transcriptions"
DEFAULT_MODEL = "gpt-4o-mini-transcribe"
MAX_TRANSCRIPT = 10_000
ATTEMPTS = 3

# The service guesses the format from the file name.
EXTENSIONS = {"audio/ogg": "ogg", "audio/webm": "webm", "audio/mp4": "m4a"}


def multipart(fields, file_field, filename, content_type, data):
    """A multipart/form-data body. Returns (body, content type header)."""
    boundary = "lightrelay" + secrets.token_hex(12)
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
                 f"Content-Type: {content_type}\r\n\r\n".encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class Permanent(Exception):
    """The service refused this file or this key; asking again won't help.
    `reason` is short enough to show under the voice note on the phone."""

    def __init__(self, message, reason):
        super().__init__(message)
        self.reason = reason


def reason_for(status, detail):
    if "insufficient_quota" in detail:
        return "No transcription credit left"
    if status in (401, 403):
        return "Transcription key refused"
    return f"Couldn't transcribe (HTTP {status})"


class Transcriber:
    """One worker thread, so notes go out one at a time and in order."""

    def __init__(self, store, api_key, url=DEFAULT_URL, model=DEFAULT_MODEL, timeout=120, retry_delay=10):
        self.store = store
        self.api_key = api_key
        self.url = url
        self.model = model
        self.timeout = timeout
        self.retry_delay = retry_delay
        self.queue = queue.Queue()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        # Anything stored while the inbox was down, or that it didn't finish.
        for attachment_id in self.store.untranscribed():
            self.queue.put(attachment_id)
        self.thread.start()
        return self

    def submit(self, attachment_id):
        self.queue.put(attachment_id)

    def _run(self):
        while True:
            attachment_id = self.queue.get()
            if attachment_id is None:
                return
            self._transcribe(attachment_id)

    def stop(self):
        self.queue.put(None)
        self.thread.join(timeout=5)

    def _transcribe(self, attachment_id):
        found = self.store.attachment(attachment_id)
        if not found or found["type"] not in EXTENSIONS:
            return
        for attempt in range(1, ATTEMPTS + 1):
            try:
                with open(os.path.join(self.store.attachments_dir, attachment_id), "rb") as f:
                    data = f.read()
                text = self.call(data, found["type"], attachment_id)
            except FileNotFoundError:
                return  # pruned meanwhile
            except Permanent as e:
                log.warning("transcribing %s refused: %s", attachment_id, e)
                self.store.add_transcript(attachment_id, "", error=e.reason)
                return
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                log.warning("transcribing %s failed (attempt %d of %d): %s", attachment_id, attempt, ATTEMPTS, e)
                if attempt < ATTEMPTS:
                    time.sleep(self.retry_delay * attempt)
                else:
                    self.store.add_transcript(attachment_id, "", error="Transcription service unavailable")
                continue
            self.store.add_transcript(attachment_id, text[:MAX_TRANSCRIPT])
            log.info("transcribed %s (%d chars)", attachment_id, len(text))
            return

    def call(self, data, content_type, attachment_id):
        # Only the fields OpenAI and xAI (/v1/stt) share: both answer JSON with "text".
        body, header = multipart({"model": self.model}, "file",
                                 f"{attachment_id}.{EXTENSIONS[content_type]}", content_type, data)
        request = urllib.request.Request(self.url, data=body, method="POST", headers={
            "Authorization": f"Bearer {self.api_key}", "Content-Type": header})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                result = json.load(resp)
        except urllib.error.HTTPError as e:
            with e:
                detail = e.read(500).decode(errors="replace")
            # 429 and 5xx pass, except a 429 for an account out of credit; anything
            # else is the file or the key.
            if (e.code == 429 and "insufficient_quota" not in detail) or e.code >= 500:
                raise urllib.error.URLError(f"HTTP {e.code} {detail}")
            raise Permanent(f"HTTP {e.code} {detail}", reason_for(e.code, detail))
        text = result.get("text") if isinstance(result, dict) else None
        if not isinstance(text, str):
            raise ValueError("no text in the answer")
        return text.strip()
