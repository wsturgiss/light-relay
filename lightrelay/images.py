"""Pictures an agent sends with a message, for the phone to show inline.

The relay takes a PNG or JPEG with POST /notify, checks it really is one, and
shrinks it to fit the Light Phone III (1080 px across) before keeping it. The
file sits in `images/` next to outbox.db, so the inbox, which mounts the relay's
data read-only, can hand it to the phone; its row is in outbox.db's `images`
table. The message carries the picture's id, size and SHA-256, all signed with
the rest, so the phone knows the bytes it downloads are the ones the relay sent.

Pillow is needed only to take a picture in (the relay); the inbox only serves
files already made, with the standard library.
"""

import base64
import binascii
import hashlib
import io
import os
import re
import secrets
import warnings

from .common import HttpError

IMAGE_ID_RE = re.compile(r"^i_[0-9a-f]{16}$")

# What an agent may send: (content type, how its bytes start).
SIGNATURES = (("image/png", b"\x89PNG\r\n\x1a\n"), ("image/jpeg", b"\xff\xd8\xff"))

IMAGES_SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    id         TEXT PRIMARY KEY,
    message_id TEXT NOT NULL,
    type       TEXT NOT NULL,
    bytes      INTEGER NOT NULL,
    width      INTEGER NOT NULL,
    height     INTEGER NOT NULL,
    sha256     TEXT NOT NULL,
    at         TEXT NOT NULL
);
"""


class Limits:
    """How big a picture may come in, and what it's made to fit. Set from the
    environment in relay.main(); these defaults suit the LP3's 1080 x 1240 screen."""
    max_upload = 15 << 20      # IMAGE_MAX_BYTES: the file as sent, before shrinking
    max_pixels = 50_000_000    # IMAGE_MAX_PIXELS: refuses decompression bombs
    max_width = 1080           # IMAGE_MAX_WIDTH
    max_height = 3600          # IMAGE_MAX_HEIGHT: about three screens of a tall picture
    target_bytes = 400_000     # IMAGE_TARGET_BYTES: what a JPEG is squeezed under
    max_stored = 1_500_000     # past this even at the lowest quality, refuse


def images_dir(outbox_db):
    return os.path.join(os.path.dirname(os.path.abspath(outbox_db)), "images")


def sniff(data):
    """The content type of PNG or JPEG bytes, else None."""
    for content_type, magic in SIGNATURES:
        if data.startswith(magic):
            return content_type
    return None


def decode_base64(value):
    """A JSON `image`: plain base64, or a `data:image/png;base64,` URL."""
    if not isinstance(value, str) or not value:
        raise HttpError(400, "image must be base64 (or a data: URL) of a PNG or JPEG")
    if value.startswith("data:"):
        header, _, value = value.partition(",")
        if ";base64" not in header:
            raise HttpError(400, "an image data: URL must be base64")
    if len(value) > (Limits.max_upload * 4) // 3 + 16:
        raise HttpError(413, f"image over {Limits.max_upload} bytes")
    try:
        return base64.b64decode("".join(value.split()), validate=True)
    except (binascii.Error, ValueError):
        raise HttpError(400, "image is not valid base64")


def prepare(data):
    """Check `data` is a PNG or JPEG and make it fit the phone.

    Returns `(bytes, content type, width, height)`. A picture that already fits,
    and is small, goes through as it is. Otherwise it's turned upright (EXIF),
    flattened onto white (a transparent picture would vanish on a dark theme),
    scaled down to the width and height limits, and saved again without its
    metadata: as PNG when that is small (a screenshot or a table stays crisp),
    else as JPEG at the best quality under the target size.
    """
    if not data:
        raise HttpError(400, "image is empty")
    if len(data) > Limits.max_upload:
        raise HttpError(413, f"image over {Limits.max_upload} bytes")
    content_type = sniff(data)
    if content_type is None:
        raise HttpError(415, "image must be a PNG or JPEG")
    try:
        from PIL import Image, ImageOps
    except ImportError:
        raise HttpError(503, "this relay can't take images: Pillow isn't installed")

    Image.MAX_IMAGE_PIXELS = Limits.max_pixels
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            img = Image.open(io.BytesIO(data))
            if img.format not in ("PNG", "JPEG"):
                raise HttpError(415, "image must be a PNG or JPEG")
            width, height = img.size
            if width * height > Limits.max_pixels:
                raise HttpError(413, f"image over {Limits.max_pixels} pixels")
            exif_rotated = img.format == "JPEG" and img.getexif().get(0x0112, 1) != 1
            if (width <= Limits.max_width and height <= Limits.max_height and not exif_rotated
                    and len(data) <= Limits.target_bytes and "transparency" not in img.info
                    and img.mode in ("RGB", "L", "P", "1")):
                img.load()  # a truncated file fails here, not on the phone
                return data, content_type, width, height
            if img.format == "JPEG":
                # Decode at a fraction of the size when that's all we need: much faster.
                img.draft("RGB", (Limits.max_width, Limits.max_height))
            img = ImageOps.exif_transpose(img)
            img.load()
    except HttpError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise HttpError(413, f"image over {Limits.max_pixels} pixels")
    except (OSError, SyntaxError, ValueError) as e:
        raise HttpError(400, f"image can't be read: {e}")

    img = flatten(img)
    scale = min(1.0, Limits.max_width / img.width, Limits.max_height / img.height)
    if scale < 1:
        img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                         Image.Resampling.LANCZOS)

    # A drawing, a screenshot or a table: PNG keeps text sharp, if it's small enough.
    if content_type == "image/png":
        png = encode_png(img)
        if len(png) <= Limits.target_bytes:
            return png, "image/png", img.width, img.height
        # Text on a plain background (a document, a long list): anti-aliasing gives it a
        # few thousand colours, and a 256-colour palette keeps it sharp where JPEG blurs it.
        if img.mode == "RGB" and img.getcolors(4096) is not None:
            png = encode_png(img.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE))
            if len(png) <= Limits.target_bytes:
                return png, "image/png", img.width, img.height
    jpeg = encode_jpeg(img, graphic=content_type == "image/png")
    if len(jpeg) > Limits.max_stored:
        raise HttpError(413, "image is still too big once shrunk")
    return jpeg, "image/jpeg", img.width, img.height


def flatten(img):
    from PIL import Image
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        white = Image.new("RGB", rgba.size, (255, 255, 255))
        white.paste(rgba, mask=rgba.getchannel("A"))
        return white
    return img.convert("L") if img.mode in ("L", "1", "I;16", "I") else img.convert("RGB")


def encode_png(img):
    out = io.BytesIO()
    # Few colours (most charts and tables): a palette is lossless and far smaller.
    if img.mode == "P":
        img.save(out, "PNG", optimize=True)
    elif img.mode == "RGB" and img.getcolors(256) is not None:
        img.quantize(colors=256, dither=0).save(out, "PNG", optimize=True)
    else:
        img.save(out, "PNG", optimize=True)
    return out.getvalue()


def encode_jpeg(img, graphic=False):
    """The best quality under the target. Graphics keep full colour resolution
    (4:4:4), which keeps coloured text from smearing."""
    best = None
    for quality in (88, 82, 76, 70, 62, 55, 48, 40):
        out = io.BytesIO()
        img.save(out, "JPEG", quality=quality,
                 subsampling=0 if graphic and quality >= 70 else 2)
        best = out.getvalue()
        if len(best) <= Limits.target_bytes:
            break
    return best


def store(directory, data):
    """Write the file under a new id. Returns (id, sha256 hex). The caller adds
    the row; until then, the file is an orphan the next prune removes."""
    os.makedirs(directory, exist_ok=True)
    image_id = "i_" + secrets.token_hex(8)
    final = os.path.join(directory, image_id)
    part = f"{final}.part"
    with open(part, "wb") as f:
        f.write(data)
    os.replace(part, final)
    return image_id, hashlib.sha256(data).hexdigest()


def remove(directory, image_ids):
    for image_id in image_ids:
        try:
            os.remove(os.path.join(directory, image_id))
        except FileNotFoundError:
            pass
