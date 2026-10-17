"""Questions: choice objects (id, label, style), select "many", the free-text Other,
and answers that lock a question (answeredAt, secondAnswer). Run with `python -m unittest`."""

import json
import sqlite3
import unittest

from lightrelay import inbox
from lightrelay.common import RateLimiter
from tests import test_end_to_end as e2e
from tests.test_end_to_end import AGENT_IP, FakePush, request


class Questions(unittest.TestCase):
    setUp_base = e2e.EndToEnd.setUp
    tearDown = e2e.EndToEnd.tearDown
    reply = e2e.EndToEnd.reply
    fetch = e2e.EndToEnd.fetch

    def setUp(self):
        self.setUp_base()
        inbox.InboxHandler.per_ip = RateLimiter(1000, 60)

    def ask(self, **body):
        status, sent = request(self.relay_url + "/notify", "POST", {"headline": "Q?", **body}, via=AGENT_IP)
        self.assertEqual(status, 202, sent)
        return sent["id"], json.loads(FakePush.received[-1].decode().split(".", 2)[2])

    def replies(self):
        return request(self.relay_url + "/replies", via=AGENT_IP)[1]["replies"]

    # the message

    def test_plain_choices_are_unchanged(self):
        _, message = self.ask(choices=["Yes", "No"])
        self.assertEqual(message["choices"], ["Yes", "No"])
        for key in ("options", "select", "other"):
            self.assertNotIn(key, message)

    def test_choice_objects_keep_labels_for_old_tools(self):
        _, message = self.ask(choices=[{"id": "ok", "label": "Approve"},
                                       {"id": "del", "label": "Delete it", "style": "danger"}],
                              select="many", other=True)
        self.assertEqual(message["choices"], ["Approve", "Delete it"])
        self.assertEqual(message["options"], [{"id": "ok", "label": "Approve", "style": "default"},
                                              {"id": "del", "label": "Delete it", "style": "danger"}])
        self.assertEqual((message["select"], message["other"]), ("many", True))

    def test_mixed_choices_get_numbered_ids(self):
        _, message = self.ask(choices=["Keep", {"label": "Drop", "style": "danger"}])
        self.assertEqual([o["id"] for o in message["options"]], ["c1", "c2"])

    def test_bad_questions_are_refused(self):
        for body in ({"choices": [{"label": "x" * 25}]},
                     {"choices": ["a", "b", "c", "d", {"label": "e"}]},
                     {"choices": [{"label": "a", "style": "loud"}]},
                     {"choices": [{"id": "x", "label": "a"}, {"id": "x", "label": "b"}]},
                     {"choices": ["a", "A"]},
                     {"choices": [{"id": "bad id", "label": "a"}]},
                     {"choices": [{"label": "a", "color": "red"}]},
                     {"choices": ["a", "b"], "select": "some"},
                     {"choices": ["a"], "select": "many"},
                     {"select": "many"}, {"other": True},
                     {"choices": ["a"], "other": "yes"}):
            status, _ = request(self.relay_url + "/notify", "POST", {"headline": "Q?", **body}, via=AGENT_IP)
            self.assertEqual(status, 400, body)

    def test_multipart_carries_select_and_other(self):
        boundary = "b0undary"
        parts = [("headline", "Q?"), ("choice", "A"), ("choice", "B"), ("select", "many"), ("other", "true")]
        body = "".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{n}"\r\n\r\n{v}\r\n'
                       for n, v in parts) + f"--{boundary}--\r\n"
        status, _ = request(self.relay_url + "/notify", "POST", body,
                            {"Content-Type": f"multipart/form-data; boundary={boundary}"}, via=AGENT_IP)
        self.assertEqual(status, 202)
        message = json.loads(FakePush.received[-1].decode().split(".", 2)[2])
        self.assertEqual((message["select"], message["other"]), ("many", True))

    # the answer

    def test_choice_ids_name_the_labels_and_lock_the_question(self):
        mid, _ = self.ask(choices=[{"id": "y", "label": "Yes"}, {"id": "n", "label": "No"}])
        status, body = self.reply({"id": "r1", "messageId": mid, "choice": "whatever", "choiceIds": ["n"]})
        self.assertEqual(status, 202)
        self.assertFalse(body["secondAnswer"])
        got = self.replies()[0]
        self.assertEqual((got["choice"], got["choiceIds"], got["other"], got["secondAnswer"]), ("No", ["n"], False, False))
        self.assertEqual(got["answeredAt"], body["answeredAt"])

    def test_a_second_answer_is_kept_but_flagged(self):
        mid, _ = self.ask(choices=["Yes", "No"])
        self.reply({"id": "r1", "messageId": mid, "choice": "Yes", "choiceIds": ["c1"]})
        status, body = self.reply({"id": "r2", "messageId": mid, "choice": "No", "choiceIds": ["c2"]})
        self.assertEqual(status, 202)
        self.assertTrue(body["secondAnswer"])
        first, second = self.replies()
        self.assertEqual((first["secondAnswer"], second["secondAnswer"]), (False, True))
        self.assertEqual(second["answeredAt"], first["answeredAt"])
        # A retry of the first is still the first.
        self.assertFalse(self.reply({"id": "r1", "messageId": mid, "choice": "Yes", "choiceIds": ["c1"]})[1]["secondAnswer"])

    def test_an_old_tool_choice_counts_as_the_answer(self):
        mid, _ = self.ask(choices=["Yes", "No"])
        self.reply({"id": "r1", "messageId": mid, "choice": "Yes"})
        self.assertTrue(self.reply({"id": "r2", "messageId": mid, "choice": "No"})[1]["secondAnswer"])
        self.assertIsNone(self.replies()[0]["choiceIds"])

    def test_plain_text_doesnt_answer(self):
        mid, _ = self.ask(choices=["Yes", "No"])
        self.reply({"id": "r1", "messageId": mid, "text": "hmm, let me think"})
        self.assertFalse(self.reply({"id": "r2", "messageId": mid, "choiceIds": ["c1"]})[1]["secondAnswer"])
        self.assertIsNone(self.replies()[0]["answeredAt"])

    def test_many_joins_labels_in_the_messages_order(self):
        mid, _ = self.ask(choices=["Milk", "Eggs", "Bread"], select="many")
        self.reply({"id": "r1", "messageId": mid, "choiceIds": ["c3", "c1"]})
        self.assertEqual(self.replies()[0]["choice"], "Milk, Bread")

    def test_other_is_text_linked_to_the_message(self):
        mid, _ = self.ask(choices=["Hike", "Market"], other=True)
        self.assertEqual(self.reply({"id": "r1", "messageId": mid, "other": True, "text": "Beach"})[0], 202)
        got = self.replies()[0]
        self.assertEqual((got["messageId"], got["text"], got["other"], got["choice"]), (mid, "Beach", True, None))
        self.assertIsNotNone(got["answeredAt"])

    def test_bad_answers_are_refused(self):
        one, _ = self.ask(choices=["Yes", "No"])
        many, _ = self.ask(choices=["A", "B"], select="many")
        for body in ({"messageId": one, "choiceIds": ["c9"]},
                     {"messageId": one, "choiceIds": ["c1", "c2"]},
                     {"messageId": one, "other": True, "text": "x"},
                     {"messageId": many, "choiceIds": ["c1", "c1"]},
                     {"messageId": many, "choiceIds": []},
                     {"messageId": many, "choiceIds": ["c1"], "other": True, "text": "x"},
                     {"choiceIds": ["c1"]},
                     {"messageId": one, "other": "yes", "text": "x"}):
            status, _ = self.reply({"id": "rbad", **body})
            self.assertEqual(status, 400, body)

    def test_nulls_from_the_phone_are_fine(self):
        mid, _ = self.ask(choices=["Yes", "No"])
        self.assertEqual(self.reply({"id": "r1", "messageId": mid, "choice": "Yes", "choiceIds": None,
                                     "other": False})[0], 202)

    def test_an_older_inbox_database_gains_the_columns(self):
        self.reply({"id": "r0", "messageId": "m_1", "choice": "Yes"})
        path = inbox.InboxHandler.store.db.execute("PRAGMA database_list").fetchone()[2]
        inbox.ReplyStore(path)
        have = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(replies)")}
        self.assertTrue({"choice_ids", "other", "answered_at", "second_answer"} <= have)


if __name__ == "__main__":
    unittest.main()
