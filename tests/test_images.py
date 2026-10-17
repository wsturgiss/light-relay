"""Pictures from the agent to the phone: POST /notify with an image, shrunk to fit."""

import base64
import hashlib
import io
import json
import os
import unittest
import urllib.error
import urllib.request

from lightrelay import agents, images, relay
from lightrelay.common import RateLimiter
from tests import test_end_to_end as e2e

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

AGENT_IP, REPLY_TOKEN, request = e2e.AGENT_IP, e2e.REPLY_TOKEN, e2e.request


def picture(width, height, mode="RGB", fmt="PNG", noisy=True, **save):
    """A test picture. Noisy ones don't compress, so they have to be shrunk."""
    if noisy:
        small = (max(1, width // 4), max(1, height // 4))
        img = Image.frombytes("RGB", small, os.urandom(small[0] * small[1] * 3)).resize((width, height))
        img = img.convert(mode)
    else:
        img = Image.new(mode, (width, height), "white")
        for x in range(0, width, 7):
            for y in range(height):
                img.putpixel((x, y), (0, 0, 0) if mode == "RGB" else 0)
    out = io.BytesIO()
    img.save(out, fmt, **save)
    return out.getvalue()


def get(url, headers=None, via=None):
    headers = dict(headers or {})
    if via:
        headers["X-Forwarded-For"] = via
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=10) as resp:
            return resp.status, resp.headers.get_content_type(), resp.read()
    except urllib.error.HTTPError as e:
        with e:
            return e.code, None, e.read()


def multipart(fields, files):
    boundary = "----lightrelaytest"
    out = io.BytesIO()
    for name, value in fields:
        out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
    for name, filename, data in files:
        out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\n"
                  f"Content-Type: application/octet-stream\r\n\r\n".encode() + data + b"\r\n")
    out.write(f"--{boundary}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


@unittest.skipIf(Image is None, "Pillow isn't installed")
class Images(unittest.TestCase):
    fetch = e2e.EndToEnd.fetch

    def setUp(self):
        e2e.EndToEnd.setUp(self)
        self.addCleanup(e2e.EndToEnd.tearDown, self)
        e2e.inbox.InboxHandler.per_ip = RateLimiter(100, 60)
        saved = {k: getattr(images.Limits, k) for k in vars(images.Limits) if not k.startswith("_")}
        self.addCleanup(lambda: [setattr(images.Limits, k, v) for k, v in saved.items()])

    def notify(self, body, content_type=None, headers=None):
        headers = dict(headers or {})
        if content_type:
            headers["Content-Type"] = content_type
        return request(self.relay_url + "/notify", "POST", body, headers, via=AGENT_IP)

    def pushed(self):
        return json.loads(e2e.FakePush.received[-1].decode().split(".", 2)[2])

    def phone_gets(self, image_id, token=REPLY_TOKEN):
        return get(self.inbox_url + f"/images/{image_id}", {"Authorization": f"Bearer {token}"}, via="203.0.113.5")

    def test_a_wide_png_is_shrunk_to_the_phones_width_and_signed(self):
        data = picture(3200, 1892)
        status, body = self.notify({"headline": "Comps", "image": base64.b64encode(data).decode()})
        self.assertEqual(status, 202, body)
        image = self.pushed()["image"]
        self.assertEqual((image["width"], image["height"]), (1080, 639))
        self.assertLessEqual(image["bytes"], images.Limits.target_bytes)
        self.assertEqual(body["image"]["path"], f"/images/{image['id']}")
        # The phone gets exactly the bytes the signed message describes.
        status, content_type, got = self.phone_gets(image["id"])
        self.assertEqual((status, content_type), (200, image["type"]))
        self.assertEqual(hashlib.sha256(got).hexdigest(), image["sha256"])
        self.assertEqual(len(got), image["bytes"])
        self.assertEqual(Image.open(io.BytesIO(got)).size, (1080, 639))
        # And the fetched body is the pushed one.
        fetched = self.fetch("/messages")[1]["messages"][0]["body"]
        self.assertEqual(fetched.encode(), e2e.FakePush.received[-1])

    def test_a_table_stays_a_crisp_png(self):
        data = picture(2400, 900, noisy=False)
        status, _ = self.notify({"headline": "Table", "image": "data:image/png;base64," + base64.b64encode(data).decode()})
        self.assertEqual(status, 202)
        image = self.pushed()["image"]
        self.assertEqual((image["type"], image["width"]), ("image/png", 1080))

    def test_a_small_picture_goes_through_as_it_is(self):
        data = picture(400, 300, fmt="JPEG", quality=80)
        self.notify({"headline": "Small", "image": base64.b64encode(data).decode()})
        image = self.pushed()["image"]
        self.assertEqual((image["type"], image["width"], image["height"]), ("image/jpeg", 400, 300))
        self.assertEqual(self.phone_gets(image["id"])[2], data)

    def test_multipart_like_curl_F(self):
        body, content_type = multipart([("headline", "Which one?"), ("detail", "Both are listed."),
                                        ("choice", "Left"), ("choice", "Right")],
                                       [("image", "a.jpg", picture(2000, 1500, fmt="JPEG", quality=95))])
        status, result = self.notify(body, content_type)
        self.assertEqual(status, 202, result)
        message = self.pushed()
        self.assertEqual((message["headline"], message["detail"], message["choices"]),
                         ("Which one?", "Both are listed.", ["Left", "Right"]))
        self.assertEqual((message["image"]["type"], message["image"]["width"], message["image"]["height"]),
                         ("image/jpeg", 1080, 810))

    def test_multipart_without_an_image_is_a_plain_message(self):
        body, content_type = multipart([("headline", "Hi")], [])
        self.assertEqual(self.notify(body, content_type)[0], 202)
        self.assertNotIn("image", self.pushed())

    def test_exif_rotation_is_applied(self):
        exif = Image.Exif()
        exif[0x0112] = 6  # rotate 90 degrees clockwise to view
        data = picture(1600, 1200, fmt="JPEG", exif=exif)
        self.notify({"headline": "Upright", "image": base64.b64encode(data).decode()})
        image = self.pushed()["image"]
        self.assertEqual((image["width"], image["height"]), (1080, 1440))

    def test_transparency_is_flattened_onto_white(self):
        img = Image.new("RGBA", (1500, 200), (0, 0, 0, 0))
        out = io.BytesIO()
        img.save(out, "PNG")
        self.notify({"headline": "Logo", "image": base64.b64encode(out.getvalue()).decode()})
        image = self.pushed()["image"]
        got = Image.open(io.BytesIO(self.phone_gets(image["id"])[2])).convert("RGB")
        self.assertEqual(got.getpixel((5, 5)), (255, 255, 255))

    def test_what_isnt_a_png_or_jpeg_is_refused(self):
        gif = b"GIF89a" + b"\x00" * 100
        self.assertEqual(self.notify({"headline": "x", "image": base64.b64encode(gif).decode()})[0], 415)
        self.assertEqual(self.notify({"headline": "x", "image": "not base64!"})[0], 400)
        self.assertEqual(self.notify({"headline": "x", "image": 42})[0], 400)
        truncated = picture(2000, 1000)[:5000]
        self.assertEqual(self.notify({"headline": "x", "image": base64.b64encode(truncated).decode()})[0], 400)
        self.assertEqual(e2e.FakePush.received, [])
        self.assertEqual(os.listdir(images.images_dir(self.tmp.name + "/outbox.db"))
                         if os.path.isdir(images.images_dir(self.tmp.name + "/outbox.db")) else [], [])

    def test_size_and_pixels_are_capped(self):
        images.Limits.max_upload = 1000
        data = picture(200, 200)
        self.assertEqual(self.notify({"headline": "x", "image": base64.b64encode(data).decode()})[0], 413)
        images.Limits.max_upload = 15 << 20
        images.Limits.max_pixels = 10_000
        self.assertEqual(self.notify({"headline": "x", "image": base64.b64encode(data).decode()})[0], 413)

    def test_the_body_cap_still_holds_for_plain_text(self):
        self.assertEqual(self.notify("x" * 20000, "text/plain")[0], 413)

    def test_only_the_phone_and_listed_agents_can_read_a_picture(self):
        self.notify({"headline": "x", "image": base64.b64encode(picture(100, 100)).decode()})
        image_id = self.pushed()["image"]["id"]
        self.assertEqual(self.phone_gets(image_id, token="nope")[0], 401)
        self.assertEqual(self.phone_gets("i_nothex")[0], 400)
        self.assertEqual(self.phone_gets("i_0000000000000000")[0], 404)
        self.assertEqual(get(self.relay_url + f"/images/{image_id}", via=AGENT_IP)[0], 200)
        self.assertEqual(get(self.relay_url + f"/images/{image_id}", via=e2e.STRANGER_IP)[0], 403)

    def test_the_channel_shows_the_picture(self):
        token = "j" * 40
        settings = agents.settings(("Jarvis",), {"AGENT_JARVIS_TOKEN": token})
        owners = agents.Owners(("Jarvis",), self.tmp.name + "/outbox.db", relay.RelayHandler.inbox_db)
        for k, v in (("agent_settings", settings), ("owners", owners)):
            self.addCleanup(setattr, relay.RelayHandler, k, getattr(relay.RelayHandler, k))
            setattr(relay.RelayHandler, k, v)
        auth = {"Authorization": f"Bearer {token}"}
        self.notify({"headline": "x", "image": base64.b64encode(picture(100, 80)).decode()}, headers=auth)
        status, channel = request(self.relay_url + "/channel", headers=auth, via=AGENT_IP)
        self.assertEqual(status, 200)
        entry = channel["entries"][-1]
        self.assertEqual((entry["image"]["width"], entry["image"]["height"]), (100, 80))
        self.assertEqual(entry["image"]["path"], f"/images/{entry['image']['id']}")

    def test_a_picture_goes_when_its_message_does(self):
        saved = relay.MAX_ROWS
        relay.MAX_ROWS = 1
        self.addCleanup(setattr, relay, "MAX_ROWS", saved)
        self.notify({"headline": "old", "image": base64.b64encode(picture(100, 100)).decode()})
        first = self.pushed()["image"]["id"]
        self.notify({"headline": "new one"})
        self.notify({"headline": "newer"})
        self.assertEqual(self.phone_gets(first)[0], 404)
        self.assertNotIn(first, os.listdir(images.images_dir(self.tmp.name + "/outbox.db")))


if __name__ == "__main__":
    unittest.main()
