import os
import tempfile
import unittest
from unittest import mock

from lightrelay import transcribe


class FakeResponse:
    def __init__(self):
        self.body = b'{"text": " Hello Huginn. "}'

    def read(self, *a):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class KeytermsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "stt-keyterms.txt")

    def write(self, text):
        with open(self.path, "w") as f:
            f.write(text)

    def test_parse(self):
        text = "# names\n\n  Huginn  \nhuginn\nOver-the-Rhine\n" + "x" * 51 + "\n" + "y" * 50 + "\n   \n#Othala\n"
        self.assertEqual(transcribe.parse_keyterms(text), ["Huginn", "Over-the-Rhine", "y" * 50])

    def test_capped_at_100(self):
        terms = transcribe.parse_keyterms("\n".join(f"t{i}" for i in range(150)))
        self.assertEqual(len(terms), 100)
        self.assertEqual(terms[-1], "t99")

    def test_example_file(self):
        here = os.path.join(os.path.dirname(__file__), "..", "examples", "stt-keyterms.txt")
        with open(here) as f:
            self.assertEqual(len(transcribe.parse_keyterms(f.read())), 57)

    def test_reread_on_change_and_missing(self):
        kt = transcribe.Keyterms(self.path)
        self.assertEqual(kt.get(), [])
        self.write("Grok\n")
        self.assertEqual(kt.get(), ["Grok"])
        self.write("Grok\nGrok Bot\n")
        self.assertEqual(kt.get(), ["Grok", "Grok Bot"])
        os.remove(self.path)
        self.assertEqual(kt.get(), [])
        self.assertEqual(transcribe.Keyterms(None).get(), [])

    def sent(self, keyterms_file):
        worker = transcribe.Transcriber(None, "xai-test", "https://example.invalid/v1/stt",
                                        "grok-voice-transcribe-2.0", keyterms_file=keyterms_file)
        with mock.patch.object(transcribe.urllib.request, "urlopen", return_value=FakeResponse()) as urlopen:
            text = worker.call(b"AUDIO", "audio/mp4", "a_0000000000000001")
        self.assertEqual(text, "Hello Huginn.")
        return urlopen.call_args[0][0].data

    def test_keyterms_language_and_format_go_before_the_file(self):
        self.write("Huginn\nD&D\nMarcus Aurelius\n")
        body = self.sent(self.path)
        file_at = body.index(b'name="file"')
        positions = [body.index(b'name="keyterm"\r\n\r\n' + t + b"\r\n") for t in (b"Huginn", b"D&D", b"Marcus Aurelius")]
        self.assertEqual(positions, sorted(positions))
        self.assertLess(body.index(b'name="model"'), positions[0])
        self.assertLess(positions[-1], body.index(b'name="language"\r\n\r\nen\r\n'))
        self.assertLess(body.index(b'name="format"\r\n\r\ntrue\r\n'), file_at)
        self.assertEqual(body.count(b'name="keyterm"'), 3)

    def test_no_file_sends_what_it_did_before(self):
        body = self.sent(self.path)
        self.assertNotIn(b"keyterm", body)
        self.assertNotIn(b'name="language"', body)
        self.assertNotIn(b'name="format"', body)
        self.assertIn(b'name="model"\r\n\r\ngrok-voice-transcribe-2.0', body)


if __name__ == "__main__":
    unittest.main()
